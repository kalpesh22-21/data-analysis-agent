"""`run_http_daemon` — the SIGTERM exit-code contract for the uvicorn-serving entrypoints.

The defect these pin (ISSUES.md C3): uvicorn's `capture_signals` RE-RAISES the SIGTERM
it captured once `serve()` returns, and the handler it restores first is `SIG_DFL` — so
the process dies by signal inside `capture_signals.__exit__`, at exit code 143, with
every line after `await server.serve()` unreachable. The graceful shutdown had already
happened; what was missing was the ability to say so. Measured before the fix, with the
old shape (a bare `await server.serve()` under `asyncio.run`): `returncode=-15`, and a
`print` placed after `serve()` never ran.

So these tests send a REAL SIGTERM (`os.kill(os.getpid(), SIGTERM)`) to a REAL uvicorn
server, because "the chained handler is what uvicorn restores and re-raises onto" is the
claim, and calling the handler directly would assert around the only mechanism involved.

The signal is always raised from a point that is provably INSIDE `capture_signals`, so
uvicorn's own `handle_exit` is installed and the test process is never at risk of taking
a default-disposition SIGTERM. There are two such points and both are used below:

  * the app's lifespan `startup` hook — `capture_signals` wraps `_serve`, which runs
    startup;
  * the app FACTORY itself, since `factory=True` means uvicorn calls it from
    `config.load()`, which `_serve` does first of all. This is the newer of the two and
    the whole reason for the factory: composition is the slowest boot phase, and it used
    to run before any handler existed at all (i.e. at `SIG_DFL`).

The factory-side test guards its own safety — it checks the installed handler is not
`SIG_DFL` before raising — so a regression that moves composition back outside the
captured region fails as an assertion instead of killing the pytest process at 143.

SYNC tests on purpose: `run_http_daemon` owns `asyncio.run`, which cannot be called from
inside a running loop (pytest-asyncio's auto mode would supply one).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
import signal
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette

from data_agent import http_daemon
from data_agent.http_daemon import run_http_daemon

_logger = logging.getLogger("test.http_daemon")

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# Every in-repo launcher that owns a `serve()` call, i.e. every process that can chain
# the re-raise. The list is the whole point: for as long as a deployed workload entered
# through the uvicorn CLI, uvicorn owned `main()`, no repo code bracketed `serve()`, and
# there was nowhere to put the fix (an import-time handler is not a substitute — it would
# land INSIDE the captured region and break the graceful shutdown itself). The last three
# holes, `runtime`/`ui`/`inbox-ui`, are closed by the first two entries here: the charts
# now run `python scripts/run_runtime_api.py` and `python scripts/run_ui_bff.py`, and
# `ui` + `inbox-ui` share the second one (they are the same app, split by env).
_UVICORN_LAUNCHERS = (
    "run_runtime_api.py",
    "run_ui_bff.py",
    "run_inbox_service.py",
    "run_ui_runtime.py",
    "run_ui_runtime_real.py",
)


def _app_factory(*, on_startup, on_shutdown=None) -> Starlette:
    """A minimal app whose lifespan hooks are plain callables.

    Written as a `lifespan` context manager rather than the older
    `on_startup=`/`on_shutdown=` keywords, which this Starlette no longer accepts.
    """

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette):
        on_startup()
        yield
        if on_shutdown is not None:
            on_shutdown()

    return Starlette(lifespan=_lifespan)


def _sigterm_self() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def _run(app_factory, **kwargs) -> int:
    """Serve on an EPHEMERAL port (0) so a developer's local stack never collides."""
    return run_http_daemon(
        app_factory,
        host="127.0.0.1",
        port=0,
        logger=_logger,
        process="test",
        **kwargs,
    )


# --- the SIGTERM path ---------------------------------------------------------


def test_sigterm_exits_zero_after_a_graceful_shutdown(caplog) -> None:
    """THE regression test for C3. Same shutdown as before; the difference is that the
    process survives to report it."""
    with caplog.at_level(logging.INFO, logger="test.http_daemon"):
        rc = _run(lambda: _app_factory(on_startup=_sigterm_self))

    assert rc == 0, "a clean SIGTERM shutdown is a SUCCESS, not 143"
    assert any("SIGTERM received" in r.getMessage() for r in caplog.records), (
        "an operator reading the logs must be able to tell this exit apart from a crash"
    )


def test_the_app_lifespan_still_runs_on_the_sigterm_path() -> None:
    """The point of exiting 0 is that the shutdown was real. uvicorn owns that shutdown
    and the wrapper must not shorten it: both lifespan hooks run, in order, before the
    exit code is decided."""
    events: list[str] = []

    def _startup() -> None:
        events.append("startup")
        _sigterm_self()

    def _factory() -> Starlette:
        return _app_factory(on_startup=_startup, on_shutdown=lambda: events.append("shutdown"))

    assert _run(_factory) == 0
    assert events == ["startup", "shutdown"]


def test_the_sigterm_handler_is_not_left_installed() -> None:
    """The handler is restored on the way out. Left installed, it would answer a later
    SIGTERM by setting `should_exit` on a server that has already stopped — i.e. the
    process would silently ignore its own stop signal. Also what lets two of these run
    in one pytest process."""
    before = signal.getsignal(signal.SIGTERM)

    assert _run(lambda: _app_factory(on_startup=_sigterm_self)) == 0

    assert signal.getsignal(signal.SIGTERM) is before


def test_two_servers_in_one_process_both_answer_their_signal() -> None:
    """The consequence of the restore above, asserted where it would actually bite."""
    assert _run(lambda: _app_factory(on_startup=_sigterm_self)) == 0
    assert _run(lambda: _app_factory(on_startup=_sigterm_self)) == 0


# --- composition happens where uvicorn can protect it -------------------------


def test_the_app_is_composed_inside_the_running_loop_and_the_captured_region() -> None:
    """The `factory=True` claim, stated as the two properties that depend on it.

    IN THE LOOP is H7: the inbox service's write plane composes `acouchbase.Cluster`,
    which raises `RuntimeError: Event loop is not running` when constructed without one.

    INSIDE `capture_signals` is C3's remaining hole: composition is the slowest boot
    phase (imports, driver setup, warm-up) and, when the wrapper called the factory
    itself, the only phase running before any SIGTERM handler existed — a stop signal
    arriving there took the default disposition and killed the pod at 143, which is the
    exact bug this module exists to close. Asserting the INSTALLED HANDLER is uvicorn's
    own `handle_exit` states that without sending a signal at all, so this test can fail
    without taking the process with it.
    """
    observed: dict = {}

    def _factory() -> Starlette:
        observed["loop"] = asyncio.get_running_loop()
        observed["handler"] = signal.getsignal(signal.SIGTERM)
        return _app_factory(on_startup=_sigterm_self)

    assert _run(_factory) == 0

    assert observed["loop"] is not None
    handler = observed["handler"]
    assert getattr(handler, "__name__", None) == "handle_exit", (
        "the app was composed outside uvicorn's captured-signal region — a SIGTERM "
        f"during composition would not be handled gracefully (handler was {handler!r})"
    )
    assert isinstance(getattr(handler, "__self__", None), uvicorn.Server)


def test_a_sigterm_during_composition_is_a_graceful_shutdown() -> None:
    """The consequence, driven with a real signal. The pod is being rolled while it is
    still building its app — the longest window in a boot, and the one a rolling update
    is most likely to land in — and that has to be a clean exit 0, not a 143."""

    def _factory() -> Starlette:
        # Self-protection: if composition ever moves back out of the captured region,
        # `_sigterm_self` below would take the DEFAULT disposition and kill the pytest
        # process. Fail as an assertion instead. (A crash in the factory propagates out
        # of `serve()` untouched — pinned by the test below this one.)
        assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN), (
            "composition is running at the default SIGTERM disposition"
        )
        _sigterm_self()
        return _app_factory(on_startup=lambda: None)

    assert _run(_factory) == 0


def test_the_loop_is_the_one_uvicorn_itself_would_have_built() -> None:
    """Loop parity with `uvicorn.run`, which is a real behaviour difference and not a
    tidiness point: `Server.run` drives the loop `Config.get_loop_factory()` names, and
    a plain `asyncio.run()` silently ignored it — so the launchers that moved onto this
    wrapper were downgraded from uvloop to the stdlib selector loop without a word.
    Pinned as "the same class uvicorn would have chosen HERE", so the test states the
    parity rather than the presence of any particular package.
    """
    observed: dict = {}

    def _factory() -> Starlette:
        observed["loop_type"] = type(asyncio.get_running_loop())
        return _app_factory(on_startup=_sigterm_self)

    assert _run(_factory) == 0

    loop_factory = uvicorn.Config(
        _factory, factory=True, host="127.0.0.1", port=0
    ).get_loop_factory()
    expected_loop = loop_factory()
    try:
        assert observed["loop_type"] is type(expected_loop)
    finally:
        expected_loop.close()

    if importlib.util.find_spec("uvloop") is not None:
        # `uvicorn[standard]` is what the images install, so this is the branch that
        # runs in production — named explicitly because "parity" above would still hold
        # if uvicorn's own choice regressed to the default loop.
        assert observed["loop_type"].__module__.startswith("uvloop")


# --- the failure path ---------------------------------------------------------


def test_a_failed_startup_hook_exits_with_the_startup_failure_code() -> None:
    """`uvicorn.main.STARTUP_FAILURE` parity. A failed lifespan `startup` does NOT raise
    out of `serve()` — uvicorn logs "Application startup failed. Exiting." and returns
    NORMALLY — so without this check a pod that never served a request would exit 0 and
    Kubernetes would mark it Completed instead of restarting it. This is the one thing
    `uvicorn.run()` does after serving, and driving `Server` by hand must not drop it.
    """

    def _boom() -> None:
        raise RuntimeError("warm-up failed")

    assert _run(lambda: _app_factory(on_startup=_boom)) == 3


def test_a_crash_in_the_app_factory_propagates() -> None:
    """The wrapper is a lifecycle policy, not an error swallower: a composition failure
    must stay a traceback, not become an exit code that reads like a clean stop."""

    def _factory() -> Starlette:
        raise RuntimeError("composition failed")

    with pytest.raises(RuntimeError, match="composition failed"):
        _run(_factory)


# --- restoring the handler we replaced ----------------------------------------


class _StubServer:
    """Enough `uvicorn.Server` for the restore path, and nothing else.

    The two tests below patch `signal.signal` itself, which a REAL server would also be
    calling (`capture_signals`) — so they drive the private `_serve` with a stub instead
    of `run_http_daemon`. That is the narrowest way to reach a branch that is otherwise
    only reachable on a process whose SIGTERM handler came from a C extension.
    """

    def __init__(self) -> None:
        self.started = True
        self.should_exit = False

    async def serve(self) -> None:
        return None


def test_a_previous_handler_that_lives_in_c_is_not_left_as_ours(monkeypatch) -> None:
    """`signal.signal` answers None when the handler it replaced was installed from C —
    a real answer, not an error, and NOT the same fact as "we never installed one". The
    old restore tested `previous is not None` and so conflated them: on such a process
    OUR handler stayed installed for the rest of its life, and a later SIGTERM would set
    `should_exit` on a server that had already stopped and otherwise vanish — a process
    that ignores its own stop signal until the grace period ends in SIGKILL, which is
    strictly worse than the 143 this module exists to remove.

    None cannot be handed back to `signal.signal`, so SIG_DFL is the restore: the
    process gets the default disposition back rather than a handler for a dead server.
    """
    installed: list[object] = []

    def _fake_signal(signum, handler):
        installed.append(handler)
        return None  # the C-handler answer

    monkeypatch.setattr(signal, "signal", _fake_signal)

    assert asyncio.run(http_daemon._serve(_StubServer(), _logger, "test")) == 0

    assert installed[-1] is signal.SIG_DFL, (
        f"our handler was left installed for the rest of the process (restored {installed[-1]!r})"
    )


def test_no_restore_is_attempted_when_the_handler_was_never_installed(caplog) -> None:
    """The other half of the sentinel. Off the main thread `signal.signal` RAISES, so
    there is nothing of ours to take back out — and calling it again in the `finally`
    would raise a second time, out of the exit path, masking the real outcome. The
    warning is the contract: this process WILL still exit 143 on a rollout."""
    import threading

    result: list[int] = []
    error: list[BaseException] = []

    def _run_off_main_thread() -> None:
        try:
            result.append(asyncio.run(http_daemon._serve(_StubServer(), _logger, "test")))
        except BaseException as exc:  # noqa: BLE001 - the point is that nothing escapes
            error.append(exc)

    with caplog.at_level(logging.WARNING, logger="test.http_daemon"):
        thread = threading.Thread(target=_run_off_main_thread)
        thread.start()
        thread.join()

    assert error == []
    assert result == [0]
    assert any("not the main thread" in r.getMessage() for r in caplog.records)


# --- adoption -----------------------------------------------------------------


@pytest.mark.parametrize("script", _UVICORN_LAUNCHERS)
def test_every_in_repo_uvicorn_launcher_goes_through_the_wrapper(script: str) -> None:
    """Checked as SOURCE, not by booting them (the real-runtime launcher preflights the
    OpenAI Responses API at import). The claim is narrow and structural: the process
    entry is `run_http_daemon`, not `uvicorn.run` and not a bare `Server.serve()` —
    which is exactly the difference between a rollout that exits 0 and one that exits
    143. Parameterized so the next in-repo launcher cannot quietly ship without it.
    """
    source = (_SCRIPTS / script).read_text(encoding="utf-8")
    assert "run_http_daemon(" in source
    assert "uvicorn.run(" not in source, (
        f"{script} still enters through uvicorn.run, whose SIGTERM re-raise lands on "
        "SIG_DFL and kills the process at 143 before any exit-code policy can apply"
    )
