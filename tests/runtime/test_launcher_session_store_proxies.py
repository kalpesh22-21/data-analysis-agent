"""Every `SessionStore` stand-in in `scripts/` must cover the whole Protocol.

The defect this locks down: the launchers cannot construct `CouchbaseSessionStore`
at module import (the `acouchbase` cluster connects EAGERLY and needs a running event
loop, while `app = build_*_app()` runs before uvicorn's), so each one wraps it in a
hand-written lazy proxy that re-declares and forwards every `SessionStore` method.
Hand-written means it drifts: a method added to the Protocol and forgotten in a proxy
raises `AttributeError` on the FIRST live call and nowhere else. That has now happened
twice — `read_full_result` (500s from `GET /session/history`) and Release 1's
`apply_analysis_state` + `claim_finalization_block`, which made `analysisState`
completely non-functional against the real server while 4760 tests passed.

Nothing in `tests/` imports `scripts/run_ui_runtime_real.py` and nothing can: importing
it builds the app, which reads `.env` and preflights the OpenAI API over the network.
So this module reads the launchers as SOURCE (`ast`) rather than importing them — the
same move `test_couchbase_connect_gate.py` makes for store discovery, and for the same
reason: what you are hunting is code nobody exercises.

Nothing here is a method list. The required surface is derived from the `SessionStore`
Protocol by introspection, and the population of proxies is derived from the source
tree (every class in `scripts/` that constructs a `CouchbaseSessionStore` is standing
in for one). A method added to the Protocol tomorrow fails here for every proxy that
forgets it, and a third launcher proxy is covered the day it is written.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore
from data_agent.runtime.session.memory_store import InMemorySessionStore
from data_agent.runtime.session.store import SessionStore

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_ROOT = _REPO_ROOT / "scripts"

# The class the proxies wrap; constructing it is what marks a class as a stand-in.
_WRAPPED_STORE = CouchbaseSessionStore.__name__

# Signature shape: (name, kind, has_default) per parameter, `self` dropped. Compared
# between the Protocol and each implementation, so one that renames a parameter or
# loses a keyword-only default fails as loudly as one that omits the method outright.
_ParamSpec = tuple[str, str, bool]

_FUNCTION_NODES = (ast.AsyncFunctionDef, ast.FunctionDef)


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


def _ast_params(node: ast.AsyncFunctionDef) -> tuple[_ParamSpec, ...]:
    """The same shape, read off a source-level `async def`."""
    args = node.args
    positional = list(args.posonlyargs) + list(args.args)
    # Defaults bind to the TAIL of the positional list.
    first_defaulted = len(positional) - len(args.defaults)
    specs: list[_ParamSpec] = []
    for index, arg in enumerate(positional):
        if arg.arg == "self":
            continue
        kind = "POSITIONAL_ONLY" if index < len(args.posonlyargs) else "POSITIONAL_OR_KEYWORD"
        specs.append((arg.arg, kind, index >= first_defaulted))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        specs.append((arg.arg, "KEYWORD_ONLY", default is not None))
    return tuple(specs)


def _constructs_wrapped_store(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and (
            (isinstance(child.func, ast.Name) and child.func.id == _WRAPPED_STORE)
            or (isinstance(child.func, ast.Attribute) and child.func.attr == _WRAPPED_STORE)
        )
        for child in ast.walk(node)
    )


def _proxy_classes() -> list[tuple[Path, ast.ClassDef]]:
    """Every class under `scripts/` that builds a `CouchbaseSessionStore`.

    That construction IS the signal: a class that builds the real store and is handed
    to `create_app(session_store=...)` is a `SessionStore` for every purpose the runtime
    has, so it owes the Protocol's full surface.
    """
    found: list[tuple[Path, ast.ClassDef]] = []
    for path in sorted(_SCRIPTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _constructs_wrapped_store(node):
                found.append((path, node))
    return found


def _methods(node: ast.ClassDef) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    return {child.name: child for child in node.body if isinstance(child, _FUNCTION_NODES)}


def _delegates_to(node: ast.AsyncFunctionDef, method_name: str) -> bool:
    """True when the body calls `self._store().<method_name>(...)`.

    Presence alone is not the contract — a proxy method that forwards to the WRONG
    inner method (copy-paste, which is how every one of these was written) is exactly
    as broken as a missing one, and just as invisible.
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute) or func.attr != method_name:
            continue
        inner = func.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "_store"
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "self"
        ):
            return True
    return False


_PROXIES = _proxy_classes()
_SURFACE = _protocol_surface()


@pytest.mark.parametrize(
    ("proxy_path", "proxy_node", "method_name"),
    [
        pytest.param(path, node, name, id=f"{path.name}::{node.name}.{name}")
        for path, node in _PROXIES
        for name in sorted(_SURFACE)
    ],
)
def test_every_launcher_proxy_covers_the_protocol(
    proxy_path: Path, proxy_node: ast.ClassDef, method_name: str
) -> None:
    """A proxy missing a Protocol method fails HERE, not on a live server."""
    methods = _methods(proxy_node)
    where = f"{proxy_path.relative_to(_REPO_ROOT)}::{proxy_node.name}"

    method = methods.get(method_name)
    assert method is not None, (
        f"{where} does not define `{method_name}`, which the SessionStore Protocol "
        "declares. This proxy is the store the launcher hands to `create_app`, so every "
        f"runtime call to `{method_name}` raises AttributeError against the live server "
        "— and no other test notices, because nothing imports scripts/. Add the "
        "delegating method."
    )
    assert isinstance(method, ast.AsyncFunctionDef), (
        f"{where}.{method_name} is a plain `def`; the Protocol declares it a coroutine "
        "and every caller awaits it."
    )
    assert _ast_params(method) == _SURFACE[method_name], (
        f"{where}.{method_name} has a signature the Protocol does not: "
        f"{_ast_params(method)} vs {_SURFACE[method_name]}. A caller written against the "
        "Protocol will pass arguments this proxy cannot accept."
    )
    assert _delegates_to(method, method_name), (
        f"{where}.{method_name} never calls `self._store().{method_name}(...)`. A proxy "
        "method that forwards to a different inner method (or to nothing) is as broken "
        "as a missing one, and fails just as silently."
    )


@pytest.mark.parametrize(
    ("store_cls", "method_name"),
    [
        pytest.param(cls, name, id=f"{cls.__name__}.{name}")
        for cls in (InMemorySessionStore, CouchbaseSessionStore)
        for name in sorted(_SURFACE)
    ],
)
def test_every_real_implementation_covers_the_protocol(store_cls: type, method_name: str) -> None:
    """The same derivation against the two in-`src/` implementations.

    They are type-checked and widely exercised, so this is cheap insurance rather than
    the sharp end — but `03-analysis-state.md` claimed there were exactly two
    implementations while the runtime had four, so no implementation gets to be covered
    only by the assumption that someone would have noticed.
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
    """Guards the guard, both halves of it.

    An empty surface or an empty proxy population makes every parametrisation above
    pass by having nothing to say. Both are derived from things that can be refactored
    out from under this module (a Protocol whose methods stopped being coroutines; a
    launcher that renamed the class it constructs), so both are asserted directly — and
    the two launchers known to carry a proxy are named, so removing the construction
    call rather than the proxy cannot quietly empty the population.
    """
    assert _SURFACE, "no public coroutines were derived from the SessionStore Protocol"
    assert {"apply_analysis_state", "claim_finalization_block"} <= set(_SURFACE)

    proxy_files = {path.name for path, _ in _PROXIES}
    assert {"run_ui_runtime.py", "run_ui_runtime_real.py"} <= proxy_files, (
        "the launcher proxies are no longer discovered by 'constructs a "
        f"CouchbaseSessionStore'. Found only: {sorted(proxy_files)}. Either the proxies "
        "were removed (delete this expectation) or the detector no longer matches them "
        "(fix it) — leaving it as-is means the real server's session store is unproven "
        "again."
    )


def test_the_proxies_stay_explicit_and_bounded() -> None:
    """No `__getattr__`, and no public method the Protocol does not declare.

    Both halves record why the delegation is kept EXPLICIT. A forwarding `__getattr__`
    would make omissions impossible — and would also make the coverage test above
    vacuous, since every missing method would resolve at runtime — while silently
    re-exporting the concrete store's private surface (`_cluster`, `_ensure_connected`,
    `connect`/`close`) into the runtime's dependency. Explicit delegation plus a derived
    coverage test is complete AND bounded. If a maintainer later prefers mechanical
    forwarding, that is a legitimate trade — but it must replace this module's premise
    deliberately, not disable it by accident.
    """
    for path, node in _PROXIES:
        where = f"{path.relative_to(_REPO_ROOT)}::{node.name}"
        methods = _methods(node)
        assert "__getattr__" not in methods, (
            f"{where} defines __getattr__, which makes the derived coverage check vacuous."
        )
        extra = {name for name in methods if not name.startswith("_")} - set(_SURFACE)
        assert not extra, (
            f"{where} exposes {sorted(extra)}, which is not on the SessionStore "
            "Protocol. Either the Protocol grew and this module's derivation is stale, "
            "or the proxy is leaking the concrete store's surface into the runtime."
        )


def test_the_signature_reader_agrees_with_inspect() -> None:
    """`_ast_params` re-implements something `inspect` already does, so it is checked
    against `inspect` on a real signature — otherwise a bug in the reader (defaults
    bound to the wrong parameters, keyword-only args dropped) would make every
    comparison above compare two wrong things and agree.

    `transition_learning_status` is the method chosen because it has both a
    keyword-only section and defaults — the two things the reader can get wrong.
    """
    node = ast.parse(
        inspect.cleandoc(inspect.getsource(CouchbaseSessionStore.transition_learning_status))
    ).body[0]
    assert isinstance(node, ast.AsyncFunctionDef)

    expected = _spec_from_signature(CouchbaseSessionStore.transition_learning_status)
    assert _ast_params(node) == expected
    assert any(kind == "KEYWORD_ONLY" for _name, kind, _default in expected)
    assert any(has_default for _name, _kind, has_default in expected)
