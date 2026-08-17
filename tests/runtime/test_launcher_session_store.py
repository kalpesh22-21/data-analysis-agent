"""The session store the launchers hand to `create_app` — the real class, directly.

This replaces `test_launcher_session_store_proxies.py`, whose subject no longer
exists. That module policed two hand-written ~110-line `_LazyCouchbaseSessionStore`
proxies in `scripts/`: the launchers could not construct the real store at module
import (`acouchbase`'s `Cluster.__init__` reaches for the running event loop, and
`app = build_*_app()` runs before uvicorn's), so each wrapped it in a stand-in that
re-declared and forwarded every `SessionStore` method. Hand-written means drift —
`read_full_result`, then Release 1's `apply_analysis_state` + `claim_finalization_block`,
each raising `AttributeError` on the FIRST live call and nowhere else — so the proxies
needed a derived AST test to stay honest.

`CouchbaseSessionStore.__init__` is now I/O-free and loop-free (`CouchbaseStoreBase`
defers the `Cluster` to the first `_ensure_connected()`), so both launchers construct
the real class and there is nothing left to drift. What remains worth proving is what
survives that deletion:

  1. the demo launcher really does build a `CouchbaseSessionStore` under
     `DEMO_SESSION_STORE=couchbase`, from a SYNCHRONOUS test (i.e. with no running
     event loop — the exact condition `uvicorn scripts.run_ui_runtime:app` imports
     under, and the one that used to raise);
  2. the two in-`src/` implementations still cover the `SessionStore` Protocol, with
     signatures derived from the Protocol rather than listed here.

`scripts/run_ui_runtime_real.py` is deliberately NOT imported (module import reads
`.env` and preflights the OpenAI API over the network); its `_build_session_store`
now calls the same constructor with the same fallbacks, and has no hand-written
surface of its own left to drift.
"""

from __future__ import annotations

import inspect

import pytest
import scripts.run_ui_runtime as demo

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.couchbase_connect import COUCHBASE_AVAILABLE
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.store import SessionStore

# Signature shape: (name, kind, has_default) per parameter, `self` dropped. Compared
# between the Protocol and each implementation, so one that renames a parameter or
# loses a keyword-only default fails as loudly as one that omits the method outright.
_ParamSpec = tuple[str, str, bool]


def _spec_from_signature(func: object) -> tuple[_ParamSpec, ...]:
    return tuple(
        (param.name, str(param.kind), param.default is not param.empty)
        for param in inspect.signature(func).parameters.values()  # type: ignore[arg-type]
        if param.name != "self"
    )


def _protocol_surface() -> dict[str, tuple[_ParamSpec, ...]]:
    """Every PUBLIC COROUTINE on the `SessionStore` Protocol, with its signature.

    Derived, never listed: the whole point is to cover the method someone forgets, and
    a hand-maintained list only ever covers the ones someone remembered.
    """
    return {
        name: _spec_from_signature(member)
        for name, member in inspect.getmembers(SessionStore, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }


_SURFACE = _protocol_surface()


@pytest.mark.skipif(
    not COUCHBASE_AVAILABLE,
    reason="Requires the 'couchbase' package (for its options types only — the store "
    "builds no cluster and opens no socket at construction).",
)
def test_the_demo_launcher_builds_the_real_store_with_no_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SYNCHRONOUS on purpose: no running loop, exactly like module import under
    uvicorn. A `CouchbaseSessionStore` that constructed its `Cluster` eagerly would
    raise `RuntimeError('Event loop is not running.')` right here — which is why this
    launcher used to carry a proxy instead."""
    monkeypatch.setenv("DEMO_SESSION_STORE", "couchbase")

    store = demo._build_session_store(RuntimeSettings(_env_file=None))

    assert isinstance(store, CouchbaseSessionStore)
    # Built, but NOT connected: no cluster, no socket, no I/O until the first request.
    assert store._cluster is None


def test_the_demo_launcher_defaults_to_the_in_memory_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default path is unchanged — Couchbase is the ONE opt-in D45 scenario."""
    monkeypatch.delenv("DEMO_SESSION_STORE", raising=False)
    store = demo._build_session_store(RuntimeSettings(_env_file=None))
    assert isinstance(store, InMemorySessionStore)


@pytest.mark.parametrize(
    ("store_cls", "method_name"),
    [
        pytest.param(cls, name, id=f"{cls.__name__}.{name}")
        for cls in (InMemorySessionStore, CouchbaseSessionStore)
        for name in sorted(_SURFACE)
    ],
)
def test_every_real_implementation_covers_the_protocol(store_cls: type, method_name: str) -> None:
    """The Protocol's surface, derived, against both implementations.

    They are type-checked and widely exercised, so this is cheap insurance rather than
    the sharp end — but `03-analysis-state.md` claimed there were exactly two
    implementations while the runtime had four, so no implementation gets to be covered
    only by the assumption that someone would have noticed. (There ARE two now: the
    proxies that made it four are gone.)
    """
    method = getattr(store_cls, method_name, None)
    assert method is not None and inspect.iscoroutinefunction(method), (
        f"{store_cls.__name__} does not implement the SessionStore coroutine `{method_name}`."
    )
    assert _spec_from_signature(method) == _SURFACE[method_name], (
        f"{store_cls.__name__}.{method_name} signature {_spec_from_signature(method)} "
        f"does not match the Protocol's {_SURFACE[method_name]}."
    )


def test_the_derivation_is_not_vacuous() -> None:
    """Guards the guard: an empty surface makes every parametrisation above pass by
    having nothing to say, and the surface is derived from a Protocol that can be
    refactored out from under this module (methods that stopped being coroutines)."""
    assert _SURFACE, "no public coroutines were derived from the SessionStore Protocol"
    assert {"apply_analysis_state", "claim_finalization_block"} <= set(_SURFACE)
