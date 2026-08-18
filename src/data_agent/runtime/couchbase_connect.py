"""How a Couchbase-backed store is BUILT and CONNECTED — the one home for both.

`CouchbaseConnectGate` is the ONE place a store waits for its own connection;
`CouchbaseStoreBase` is the ONE place its `Cluster`/bucket/collection handles are
constructed, deferred to the gate's first await so a store is constructible outside a
running event loop. `acouchbase.Cluster.__init__` only STARTS the bootstrap, and the SDK
then refuses EVERY operation — KV and N1QL alike — until `on_connect()` has been
awaited, which a synchronous `__init__` cannot do.

INVARIANTS this mixin establishes:

  1. No public coroutine on a Couchbase-backed store may touch a cluster, bucket, or
     collection handle before `await self._ensure_connected()`. Enforced by
     introspection, not review: `tests/runtime/test_couchbase_connect_gate.py` drives
     every public coroutine of every store class against a double that reproduces the
     SDK's precondition.
  2. `_ensure_connected` is IDEMPOTENT and safe from concurrent tasks — concurrent
     first-callers await the SAME connect future rather than opening a second
     connection. `_connected` is a fast path, not a mutex; no lock is taken.
  3. A FAILED connect is never cached, so a store built against a down cluster retries
     on the next call instead of being permanently poisoned.

Accepted cost of the deferred construction: a bad connection string or credential
surfaces on the first request rather than at boot. `connect()` is the opt-in for
boot-time failure with the same attributable error.
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
    """Mixin giving a Couchbase-backed store `_ensure_connected` / `connect` / `close`.

        The subclass (in practice `CouchbaseStoreBase`) registers its `Cluster` and bucket
        via `_init_connect_gate` once they exist — at `__init__` when a ready cluster was
        injected, otherwise at the first `_ensure_connected()`. Everything asynchronous is
        deferred to that first awaited method.
    """

    _connect_targets: tuple[Any, ...]
    _connected: bool

    def _init_connect_gate(self, cluster: Any, bucket: Any) -> None:
        """Register the handles whose connect must complete before any op.

                BOTH are registered, cluster first, on purpose. `bucket.on_connect()` alone
                would suffice in the current SDK (the bucket-open request is chained onto the
                cluster connect future), but a cluster-level auth or endpoint failure would then
                surface as a bucket-open error instead of reading like a bad credential.
        """
        self._connect_targets = (cluster, bucket)
        self._connected = False

    async def _ensure_connected(self) -> None:
        """Block until this store's cluster and bucket are connected (invariant 1).

                Call this at the TOP of every public coroutine — including ones that only issue
                N1QL, which fail exactly the same way KV ops do.

                A registered handle with no `on_connect`, or one whose `on_connect()` returns
                something not awaitable, is SKIPPED: that is a unit-test double, not the SDK, and
                the unit suite must stay hermetic and importable with no `couchbase` package at
                all. That skip is fail-OPEN, so it is logged with the handle's type — were it
                ever to fire in production the symptom would land at the NEXT operation instead,
                with nothing at the skip site to find.
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

                Optional — every public method gates itself, so nothing breaks if this is never
                called. It exists for callers that want a bad endpoint or credential to fail at
                startup with an attributable error rather than mid-work.
        """
        await self._ensure_connected()

    async def close(self) -> None:
        """Release the cluster's resources (the public counterpart of `connect`).

                Closing resets `_connected`, so this store reports itself unconnected
                afterwards. The SDK does NOT support reconnecting a closed cluster, so a closed
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

        Two semantics exist in this codebase and they are NOT interchangeable, so the choice
        is a parameter: by default a bare `timedelta`, always applied; with
        `none_when_not_positive=True` a non-positive setting means "no expiry at all" and
        yields `None`, which the caller turns into options built WITHOUT `expiry=`. Passing
        `timedelta(0)` there would mean the opposite, since the SDK reads a zero expiry as
        "clear the TTL".
    """
    if none_when_not_positive and seconds <= 0:
        return None
    return timedelta(seconds=seconds)


async def get_or_none(collection: Any, key: str) -> Any | None:
    """One KV read: the SDK `GetResult`, or `None` when the document is absent.

        Returns the RESULT, not the content: callers need `content_as[dict]`, and
        `CouchbaseSessionStore` also needs `result.cas`.

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

        The five stores differ only in DATA (credentials, bucket name, TTL semantics) except
        for which collections they open, which is `_bind_collections`.

        The `cluster=` parameter every store exposes is a TEST SEAM, and that path stays
        EAGER: an injected cluster is already constructed, so its collections are derived in
        `__init__` — a fake whose `collection()` is a one-shot `side_effect` still sees the
        same two calls, in the same order. Only the settings-derived cluster is deferred,
        because only it needs a running event loop.
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
                `_bind_collections` reads. Does no I/O when *cluster* is None — it only records
                what the first connect will need.
        """
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
                first-callers cannot both build a cluster.

                `_cluster` ends up non-None only when the WHOLE build succeeded — cluster and
                handles. A partial build must not be kept: with `_cluster` set but
                `_connect_targets` still empty, the gate below would find nothing to await,
                declare itself connected, and every later call would `AttributeError` on a
                collection that was never bound. A failure anywhere unwinds to "never built" and
                the next call retries, matching invariant 3.
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
