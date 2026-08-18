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

The signal is always raised from inside the app's lifespan `startup` hook — the earliest
point that is provably INSIDE `capture_signals` (it wraps `_serve`, which runs startup),
so uvicorn's own `handle_exit` is installed and the test process is never at risk of
taking a default-disposition SIGTERM.

SYNC tests on purpose: `run_http_daemon` owns `asyncio.run`, which cannot be called from
inside a running loop (pytest-asyncio's auto mode would supply one).
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
from pathlib import Path

import pytest
from starlette.applications import Starlette

from data_agent.http_daemon import run_http_daemon

_logger = logging.getLogger("test.http_daemon")

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# The in-repo launchers that own their own `serve()` call and can therefore chain the
# re-raise. NOT here, and deliberately: `runtime`, `ui` and `inbox-ui` are deployed as
# `uvicorn <module>:<app>`, where uvicorn owns `main()` and no repo code brackets
# `serve()` — see the `http_daemon` module docstring for why an import-time handler is
# not a substitute (it would land INSIDE the captured region and break the graceful
# shutdown itself).
_UVICORN_LAUNCHERS = (
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


# --- adoption -----------------------------------------------------------------


@pytest.mark.parametrize("script", _UVICORN_LAUNCHERS)
def test_every_in_repo_uvicorn_launcher_goes_through_the_wrapper(script: str) -> None:
    """Checked as SOURCE, not by booting them (the real-runtime launcher preflights the
    OpenAI Responses API at import). The claim is narrow and structural: the process
    entry is `run_http_daemon`, not `uvicorn.run` and not a bare `Server.serve()` —
    which is exactly the difference between a rollout that exits 0 and one that exits
    143. Parameterized so a fourth in-repo launcher cannot quietly ship without it.
    """
    source = (_SCRIPTS / script).read_text(encoding="utf-8")
    assert "run_http_daemon(" in source
    assert "uvicorn.run(" not in source, (
        f"{script} still enters through uvicorn.run, whose SIGTERM re-raise lands on "
        "SIG_DFL and kills the process at 143 before any exit-code policy can apply"
    )
