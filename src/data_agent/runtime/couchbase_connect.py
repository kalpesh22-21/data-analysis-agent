"""CouchbaseConnectGate — the ONE place a Couchbase-backed store waits for its
own connection, so no caller has to know the SDK's lifecycle.

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

What this gate deliberately does NOT do: it does not make CONSTRUCTION safe
outside a running event loop. `Cluster.__init__` reaches for the running loop
(`ClientAdapter._get_loop` raises `RuntimeError('Event loop is not running.')`),
which is why `scripts/run_ui_runtime_real.py` and `scripts/run_ui_runtime.py`
wrap the session store in a lazy-construction proxy. Deferring the `Cluster(...)`
call into this gate as well would make those proxies redundant, but it would also
move a bad-connection-string failure from server boot to the first request, so it
is left as a deliberate follow-up rather than folded in here.
"""

from __future__ import annotations

import inspect
from typing import Any


class CouchbaseConnectGate:
    """Mixin giving a Couchbase-backed store `_ensure_connected` / `connect` /
    `close`.

    The subclass builds its own `Cluster` and bucket in `__init__` (the SDK's
    handle graph is sync to construct) and then registers them here via
    `_init_connect_gate`. Everything asynchronous — the actual connect — is
    deferred to the first awaited method.
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
        """
        if self._connected:
            return
        for target in self._connect_targets:
            on_connect = getattr(target, "on_connect", None)
            if on_connect is None:
                continue
            pending = on_connect()
            if inspect.isawaitable(pending):
                await pending
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
        cluster, _bucket = self._connect_targets
        closer = getattr(cluster, "close", None)
        if closer is None:
            return
        pending = closer()
        if inspect.isawaitable(pending):
            await pending
