"""The two chart launchers that replaced the bare `uvicorn <module>:<app>` commands.

`scripts/run_runtime_api.py` and `scripts/run_ui_bff.py` exist for one reason (ISSUES.md
C3): the uvicorn CLI re-raises the SIGTERM it captured onto the `SIG_DFL` it restores
after `serve()` returns, so a *successful* graceful shutdown still killed the pod at 143.
`tests/test_http_daemon.py` owns that exit-code contract, end to end, with real signals.
What is left to pin is the part those tests cannot see: that these two files are wired to
the right apps and are safe to *import*.

NO SERVER IS STARTED HERE and no port is bound. Both scripts are loaded by path, the way
they are run in the image — which is also the only way to load them, since `scripts/` is
not a package and `run_ui_bff` manipulates `sys.path` on import.

The runtime factory is exercised through a patched `create_app` rather than by calling
it for real: `create_app()` with default settings builds a `CouchbaseSessionStore`, an
OpenAI client and a tracer provider it installs GLOBALLY. Calling it in a unit test would
be testing `create_app`, not the launcher, and would leak that provider into the rest of
the session. The BFF factory is called for real — `ui.server` builds its app at import
with no I/O.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(script: str) -> ModuleType:
    """Import a `scripts/` file by path, under a name that cannot collide with the real
    module namespace (these are entrypoints, not library modules)."""
    name = f"_launcher_under_test_{script}"
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{script}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture(scope="module")
def runtime_api() -> ModuleType:
    return _load("run_runtime_api")


@pytest.fixture(scope="module")
def ui_bff() -> ModuleType:
    return _load("run_ui_bff")


# --- the flags the charts pass -------------------------------------------------


def test_the_runtime_launcher_takes_the_host_and_port_the_chart_passes(runtime_api) -> None:
    """`python scripts/run_runtime_api.py --host 0.0.0.0 --port 8000` — the flags are the
    literal replacement for the ones the Deployment used to hand uvicorn, so a template
    that keeps passing them must keep working."""
    args = runtime_api._parse_args(["--host", "0.0.0.0", "--port", "8000"])
    assert (args.host, args.port) == ("0.0.0.0", 8000)


def test_the_ui_launcher_takes_the_host_and_port_the_chart_passes(ui_bff) -> None:
    args = ui_bff._parse_args(["--host", "0.0.0.0", "--port", "3000"])
    assert (args.host, args.port) == ("0.0.0.0", 3000)


@pytest.mark.parametrize(
    ("fixture_name", "port"), [("runtime_api", 8000), ("ui_bff", 3000)]
)
def test_the_default_bind_is_loopback(request, fixture_name: str, port: int) -> None:
    """A bare `python scripts/run_*.py` — a developer's local run, or a chart edit that
    drops the flags — must not publish on every interface. The chart says 0.0.0.0 out
    loud in the pod spec; the code default does not."""
    args = request.getfixturevalue(fixture_name)._parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == port


# --- the factories -------------------------------------------------------------


def test_the_ui_factory_returns_the_bff_app(ui_bff) -> None:
    """The same object `uvicorn ui.server:app` served. Called for real (no I/O), and
    checked as an ASGI callable rather than by `isinstance`, because what uvicorn
    requires of a factory's return value is exactly that."""
    from ui.server import app as bff_app

    built = ui_bff.create_ui_app()
    assert built is bff_app
    assert callable(built)


def test_the_runtime_factory_delegates_to_create_app(runtime_api, monkeypatch) -> None:
    """Two claims in one: the factory builds the runtime app via `create_app` (the same
    entry `--factory` used), and it resolves that name at CALL time. The second is what
    the monkeypatch proves — a module-scope `from ... import create_app` would hold its
    own reference and this test would fail. It matters: the import of
    `data_agent.runtime.app` is seconds of work, and at module scope those seconds run
    before `run_http_daemon` has installed any SIGTERM handler."""
    sentinel = object()
    monkeypatch.setattr("data_agent.runtime.app.create_app", lambda: sentinel)

    assert runtime_api.create_runtime_app() is sentinel


# --- nothing runs at import ----------------------------------------------------


@pytest.mark.parametrize("script", ["run_runtime_api", "run_ui_bff"])
def test_no_app_is_built_at_module_scope(script: str) -> None:
    """Stated structurally, the way `run_inbox_service.py`'s H7 guard is (see
    `tests/learning/test_entrypoint_wiring.py`), so the next edit cannot reintroduce a
    module-level `app = create_app()` in some other guise. Module scope may bind imports,
    constants and defs — plus the module logger and the BFF's `sys.path` insert, both
    allowlisted below. Anything else RUNS at import, i.e. outside the loop and before any
    SIGTERM handler exists, which is the whole thing `factory=True` moved inside.
    """
    tree = ast.parse((_SCRIPTS / f"{script}.py").read_text(encoding="utf-8"))

    def _is_module_docstring(index: int, node: ast.stmt) -> bool:
        # ONLY the leading docstring — exempting `ast.Expr` wholesale would wave through
        # a bare `create_app()`, which is itself an Expr.
        return index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)

    def _is_main_guard(node: ast.stmt) -> bool:
        return isinstance(node, ast.If) and ast.unparse(node.test) in (
            "__name__ == '__main__'",
            "'__main__' == __name__",
        )

    allowed = {
        "_logger = logging.getLogger(__name__)",
        # The BFF's repo-root insert; see the dedicated test below for why it exists.
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))",
    }
    executable = [
        node
        for index, node in enumerate(tree.body)
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef))
        and not _is_module_docstring(index, node)
        and not _is_main_guard(node)
    ]
    offenders = [
        ast.unparse(node)
        for node in executable
        if any(isinstance(sub, ast.Call) for sub in ast.walk(node))
        and ast.unparse(node) not in allowed
    ]
    assert offenders == [], f"these run at import time in {script}.py: {offenders}"


# The app module each launcher must NOT import until its factory is called, and the
# factory that owns that import. Prefixes, so `import data_agent.runtime.app`,
# `from data_agent.runtime.app import create_app` and `from ui import server` are all
# the same finding.
_DEFERRED_IMPORTS = {
    "run_runtime_api": ("data_agent.runtime", "create_runtime_app"),
    "run_ui_bff": ("ui", "create_ui_app"),
}


def _imported_modules(node: ast.stmt) -> list[str]:
    """The dotted module names an import statement NAMES (not what it binds)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def _names_module(node: ast.stmt, prefix: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for name in _imported_modules(node))


@pytest.mark.parametrize("script", sorted(_DEFERRED_IMPORTS))
def test_the_heavy_import_lives_inside_the_factory_not_at_module_scope(script: str) -> None:
    """The guard above exempts imports wholesale — deliberately, since a launcher is
    mostly imports — which leaves the regression that matters uncovered: hoisting
    `from ui.server import app` / `from data_agent.runtime.app import create_app` to
    module scope passes every other test in this file while re-opening the exact window
    C3 closed. Importing either module is SECONDS of work (fastapi, opentelemetry, the
    couchbase extension, sqlglot; `ui.server` builds its app AS it imports), and at module
    scope those seconds run before `run_http_daemon` has installed anything — a SIGTERM
    arriving there still dies at 143 on the default disposition.

    So the placement is asserted directly, from both sides: absent at module scope, and
    PRESENT in the factory's own body. Only the second half distinguishes a correct
    launcher from one whose import was deleted along with its factory.
    """
    prefix, factory_name = _DEFERRED_IMPORTS[script]
    tree = ast.parse((_SCRIPTS / f"{script}.py").read_text(encoding="utf-8"))

    hoisted = [ast.unparse(node) for node in tree.body if _names_module(node, prefix)]
    assert hoisted == [], (
        f"{script}.py imports {prefix} at MODULE scope ({hoisted}) — that import runs "
        "before any SIGTERM handler exists, so a stop arriving during it exits 143. "
        f"Move it inside {factory_name}(), which uvicorn calls from `config.load()` "
        "inside `serve()`."
    )

    factory = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == factory_name
        ),
        None,
    )
    assert factory is not None, f"{script}.py no longer defines {factory_name}()"
    deferred = [
        ast.unparse(node) for node in ast.walk(factory) if _names_module(node, prefix)
    ]
    assert deferred, (
        f"{factory_name}() in {script}.py does not import {prefix} — the factory has to "
        "be what builds the app, or `factory=True` is deferring nothing"
    )


def test_the_ui_launcher_puts_the_repo_root_on_the_path(monkeypatch) -> None:
    """`ui/` ships as source at the repo root and is NOT part of the installed package.
    Under the uvicorn CLI the working directory carried it; `python scripts/run_ui_bff.py`
    prepends the SCRIPT's directory instead, so without the explicit insert the image
    fails at boot with `ModuleNotFoundError: ui`.

    The path is REMOVED and the module re-executed, because the test session puts the
    repo root on `sys.path` for its own reasons — asserting on the ambient path would
    pass with the line deleted. `monkeypatch.setattr` swaps in a fresh list, so the real
    one is restored intact.
    """
    root = str(_SCRIPTS.parent)
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry not in (root, "")])
    assert root not in sys.path

    _load("run_ui_bff")

    assert root in sys.path
