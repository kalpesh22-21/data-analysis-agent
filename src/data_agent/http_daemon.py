"""The process-lifecycle wrapper the uvicorn-serving entrypoints run under.

`daemon.py::run_daemon` does this for the four non-HTTP workers. This is the same
policy — **a rollout is a success, so a SIGTERM shutdown exits 0** — for the servers,
where the mechanism is completely different because uvicorn already handles the signal
and the fight is over what happens AFTER it.

WHAT UVICORN ACTUALLY DOES (0.49, `uvicorn/server.py`)::

    async def serve(self, sockets=None):
        with self.capture_signals():
            await self._serve(sockets)

    async def _serve(self, sockets=None):
        config = self.config
        if not config.loaded:
            config.load()          # <-- imports/calls the app
        ...

`capture_signals` installs `signal.signal(sig, self.handle_exit)` for SIGINT+SIGTERM,
saving whatever was there before. `handle_exit` records the signal and sets
`should_exit`, which the tick loop notices, so the graceful shutdown is uvicorn's and
it is correct. Then, on the way OUT of the context manager — after the server has
stopped cleanly — it restores the original handlers and does::

    for captured_signal in reversed(self._captured_signals):
        signal.raise_signal(captured_signal)

The comment there says it is "trying to trigger the expected behaviour now", and it
does exactly that. For SIGINT the restored handler is Python's `default_int_handler`,
so the re-raise becomes a `KeyboardInterrupt` out of `serve()` — catchable, which is
why every launcher already catches it. For SIGTERM the restored handler is `SIG_DFL`,
and the default disposition of SIGTERM is *terminate the process*. So `raise_signal`
does not return: the process dies right there, inside `capture_signals.__exit__`,
with wait-status "killed by SIGTERM" = shell code 143.

**Nothing after `await server.serve()` can run.** No `return 0`, no `sys.exit`, no
`finally`, no `atexit`. An exit-code policy written in `main()` is unreachable code on
this path, which is why the inbox/runtime/ui pods exited 143 through every rollout
while the four workers exited 0 (ISSUES C3).

THE FIX IS A CHAINED HANDLER, INSTALLED FIRST. We install our own SIGTERM handler
BEFORE calling `serve()`. uvicorn then saves OURS as the "original", and the restore +
re-raise at the end runs OURS instead of `SIG_DFL` — a Python-level handler, so the
signal is delivered, nothing dies, `serve()` returns normally, and the caller's exit
code is reached. uvicorn's own graceful shutdown is untouched: between `capture_signals`
entry and exit, `handle_exit` is the installed handler and ours is not called at all.

THE APP IS BUILT BY UVICORN, NOT BY US (`factory=True`). The `Config` is constructed
out here — `Config.__init__` only validates arguments and configures logging, it never
touches a loop — and it is handed the FACTORY rather than an app. uvicorn then calls it
from `config.load()`, which runs inside `_serve`, which buys two properties at once:

  * **in the loop.** `load()` is awaited from inside `asyncio_run`, which is what the
    inbox service needs — its full write plane composes `acouchbase.Cluster`, and that
    raises `RuntimeError: Event loop is not running` when constructed without one (H7).
  * **inside `capture_signals`.** Composition is the SLOWEST boot phase (imports,
    driver setup, warm-up) and it used to be the one phase running before any handler
    of ours existed, i.e. at `SIG_DFL`: a SIGTERM arriving while a pod was still
    building its app killed it by signal at 143, which is the exact bug this module
    exists to close. Now that same signal is `handle_exit`, so uvicorn boots, notices
    `should_exit`, and shuts down gracefully.

The residual unprotected window is what is left in front of that: `Config`
construction and the handful of statements between installing the handler and entering
`serve()`. `Config.__init__` reads its arguments and calls `logging.config.dictConfig`;
there is no I/O and nothing to block on, so what is left is bounded by argument
validation and a `dictConfig` rather than by imports, drivers or the network.

**The handler is not a no-op, and that is not incidental.** It sets
`server.should_exit`, which makes it correct in the one window where it is the only
handler installed: after we install it and before uvicorn captures. A no-op handler
would SWALLOW a signal arriving there and leave a server that ignores its stop signal
until the grace period runs out in SIGKILL — strictly worse than the bug being fixed.
Setting `should_exit` means uvicorn boots and immediately stops, which is what was
asked for.

**Nothing is logged from inside the handler.** It runs re-entrantly at a bytecode
boundary in the main thread (`signal.signal`, not `loop.add_signal_handler` — we have
to match what uvicorn saves and restores), so it touches one attribute and returns; the
operator-facing line is emitted after `serve()` returns, where it is a normal log call.

**SIGINT stays the interactive path.** The `KeyboardInterrupt` uvicorn re-raises is
caught here and mapped to 0 for exactly the reason `uvicorn.run()` swallows it: a dev's
Ctrl-C should not print a traceback over a clean shutdown.

NOT COVERED — the CLI-launched servers. `runtime`, `ui` and `inbox-ui` are deployed as
`uvicorn <module>:<app>`, so uvicorn owns `main()` and there is no in-repo code around
`serve()` to install a handler in. An import-time handler is NOT a substitute and must
not be attempted: `capture_signals` is entered BEFORE `config.load()` imports the app,
so a handler installed at import would land INSIDE the captured region — clobbering
`handle_exit` and breaking the graceful shutdown itself. Those three keep exiting 143
until their Deployment `command` moves to a `python scripts/...` launcher that can call
this wrapper.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from typing import Any

import uvicorn

# PRIVATE, on purpose: this is the same helper `uvicorn.Server.run` uses, and using it
# is the whole point — it is `asyncio.run` with a `loop_factory` on the Pythons where
# that keyword does not exist yet. Pinned to the vendored uvicorn (0.49); if a future
# bump moves it, the import fails loudly at import time rather than silently dropping
# back to the default loop, which is the failure mode being avoided.
from uvicorn._compat import asyncio_run

__all__ = ["run_http_daemon"]

# `uvicorn.main.STARTUP_FAILURE`. A failed lifespan `startup` hook does NOT raise out of
# `serve()` — uvicorn logs "Application startup failed. Exiting." and returns normally —
# so without this a server that never served a request would exit 0 and Kubernetes would
# mark the pod Completed instead of restarting it. `uvicorn.run()` ends in the same
# check; driving `Server` by hand is otherwise identical and must not drop it.
_STARTUP_FAILURE = 3

# "we never got as far as installing a handler", which is NOT the same thing as
# `signal.signal` answering None. None is a real, reachable answer — it is what the API
# returns when the previous handler was installed from C and therefore has no Python
# object to hand back. Conflating the two (the old `if previous is not None` restore)
# meant that on a process whose SIGTERM came from a C extension, OUR handler was left
# installed for the rest of the process's life: a later SIGTERM would set `should_exit`
# on a server that had already stopped and otherwise be swallowed, i.e. the process
# would ignore its own stop signal until SIGKILL.
_NOT_INSTALLED = object()


async def _serve(
    server: uvicorn.Server,
    logger: logging.Logger,
    process: str,
) -> int:
    """Serve *server* with the SIGTERM re-raise chained, and return an exit code."""
    sigterm_seen = False

    def _on_sigterm(signum: int, frame: Any) -> None:
        # Called in exactly two situations, and it has to be right in both:
        #   1. BEFORE uvicorn captures (or after it restores, at the re-raise) — the
        #      only handler installed, so asking the server to stop is on us;
        #   2. never, while uvicorn's `handle_exit` is installed.
        # No logging, no I/O: this runs re-entrantly at a bytecode boundary.
        nonlocal sigterm_seen
        sigterm_seen = True
        server.should_exit = True

    previous: Any
    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        # `signal.signal` outside the main thread. Say so rather than pretending: on
        # this path uvicorn's own restore puts SIG_DFL back and the 143 exit returns.
        previous = _NOT_INSTALLED
        logger.warning(
            "%s: could not install a SIGTERM handler (not the main thread) — a "
            "rollout will still exit 143",
            process,
        )

    try:
        await server.serve()
    finally:
        if previous is not _NOT_INSTALLED:
            # None means the prior handler lives in C and cannot be handed back to
            # `signal.signal` (it rejects None). SIG_DFL is the nearest restorable
            # thing and is the honest choice: it gives the process back the default
            # disposition instead of leaving our stopped-server handler in place.
            signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)

    if not server.started:
        # Distinguishable from the shutdown path on purpose: this pod must restart.
        logger.error("%s: startup failed — exiting %d", process, _STARTUP_FAILURE)
        return _STARTUP_FAILURE
    if sigterm_seen:
        logger.info(
            "%s: SIGTERM received — graceful shutdown complete, exiting 0 "
            "(a rollout is not a container error)",
            process,
        )
    return 0


def run_http_daemon(
    app_factory: Callable[[], Any],
    *,
    host: str,
    port: int,
    logger: logging.Logger,
    process: str,
    log_level: str | None = None,
) -> int:
    """`uvicorn.run(app_factory, factory=True, ...)`, but a SIGTERM shutdown exits 0.

    *app_factory* is handed to uvicorn as a `factory=True` app, so it is called by
    `config.load()` from inside `serve()`: in the running loop (load-bearing for the
    inbox service, whose write plane composes `acouchbase.Cluster` — which raises
    `RuntimeError: Event loop is not running` when constructed without one) and inside
    uvicorn's captured-signal region (so a SIGTERM during composition becomes a
    graceful shutdown instead of a 143). See the module docstring.

    The loop is uvicorn's own choice — `Config.get_loop_factory()` fed to the same
    `asyncio_run` `Server.run` uses — so a process started through here and the same
    app started through `uvicorn ...` are on the same event loop implementation
    (uvloop, wherever `uvicorn[standard]` is installed).

    Returns 0 for a clean stop (SIGTERM or Ctrl-C), or `uvicorn.main.STARTUP_FAILURE`
    when the lifespan `startup` hook failed. Every other exception propagates —
    including anything *app_factory* raises, which uvicorn lets out of `serve()`
    untouched (the one exception being a `TypeError`, which it reports as a bad factory
    signature and turns into `SystemExit(1)`).

    ONE KNOWN DIVERGENCE FROM `uvicorn.run`, interactive-only: a Ctrl-C during a boot
    that then FAILS its startup hook exits 0 here, where `uvicorn.run` catches the
    `KeyboardInterrupt` and still falls through to `sys.exit(STARTUP_FAILURE)`. The
    `KeyboardInterrupt` short-circuits the `server.started` check below. It cannot
    happen under a supervisor (nothing sends SIGINT to a pod), and "the operator
    stopped it themselves" is arguably the more truthful of the two exit codes, so it
    is documented rather than fixed.
    """
    config_kwargs: dict[str, Any] = {"host": host, "port": port}
    if log_level is not None:
        config_kwargs["log_level"] = log_level

    # Both built OUT here, exactly as `uvicorn.run` does: neither constructor touches a
    # loop, and the `Config` has to exist before the loop because it is what names the
    # loop factory. The APP is NOT built here — `factory=True` defers that to
    # `config.load()` inside `serve()`.
    config = uvicorn.Config(app_factory, factory=True, **config_kwargs)
    server = uvicorn.Server(config)

    async def _main() -> int:
        return await _serve(server, logger, process)

    try:
        return asyncio_run(_main(), loop_factory=config.get_loop_factory())
    except KeyboardInterrupt:
        # uvicorn captured the SIGINT, shut down cleanly, and re-raised it out of
        # `serve()` (see the module docstring). `uvicorn.run()` swallows it; so do we,
        # or a dev's Ctrl-C prints a traceback over an otherwise clean shutdown.
        logger.info("%s: interrupted — shutdown complete", process)
        return 0
