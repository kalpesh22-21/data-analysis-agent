"""The process-lifecycle wrapper the uvicorn-serving entrypoints run under.

`daemon.py::run_daemon` does this for the four non-HTTP workers. This is the same
policy — **a rollout is a success, so a SIGTERM shutdown exits 0** — for the servers,
where the mechanism is completely different because uvicorn already handles the signal
and the fight is over what happens AFTER it.

WHAT UVICORN ACTUALLY DOES (0.49, `uvicorn/server.py`)::

    async def serve(self, sockets=None):
        with self.capture_signals():
            await self._serve(sockets)

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

**The handler is not a no-op, and that is not incidental.** It sets
`server.should_exit`, which makes it correct in the one window where it is the only
handler installed: after we install it and before uvicorn captures (config load, app
import, socket bind). A SIGTERM there used to kill the process outright; a no-op
handler would instead SWALLOW it and leave a server that ignores its stop signal until
the grace period runs out in SIGKILL — strictly worse than the bug being fixed. Setting
`should_exit` means uvicorn boots and immediately stops, which is what was asked for.

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

import asyncio
import logging
import signal
from collections.abc import Callable
from typing import Any

import uvicorn

__all__ = ["run_http_daemon"]

# `uvicorn.main.STARTUP_FAILURE`. A failed lifespan `startup` hook does NOT raise out of
# `serve()` — uvicorn logs "Application startup failed. Exiting." and returns normally —
# so without this a server that never served a request would exit 0 and Kubernetes would
# mark the pod Completed instead of restarting it. `uvicorn.run()` ends in the same
# check; driving `Server` by hand is otherwise identical and must not drop it.
_STARTUP_FAILURE = 3


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

    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        # `signal.signal` outside the main thread. Say so rather than pretending: on
        # this path uvicorn's own restore puts SIG_DFL back and the 143 exit returns.
        previous = None
        logger.warning(
            "%s: could not install a SIGTERM handler (not the main thread) — a "
            "rollout will still exit 143",
            process,
        )

    try:
        await server.serve()
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)

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
    """`uvicorn.run(app_factory(), ...)`, but a SIGTERM shutdown exits 0.

    *app_factory* is called INSIDE the running loop, not by the caller. That is load-
    bearing for the inbox service, whose full write plane composes `acouchbase.Cluster`
    — which raises `RuntimeError: Event loop is not running` when constructed without
    one — and it costs the two UI launchers nothing (their apps are loop-agnostic).

    Returns 0 for a clean stop (SIGTERM or Ctrl-C), or `uvicorn.main.STARTUP_FAILURE`
    when the lifespan `startup` hook failed. Every other exception propagates.
    """
    config_kwargs: dict[str, Any] = {"host": host, "port": port}
    if log_level is not None:
        config_kwargs["log_level"] = log_level

    async def _main() -> int:
        # Both the app AND the server are built here, inside the loop — see the
        # docstring above for why the app has to be, and the `Config` holds the app.
        server = uvicorn.Server(uvicorn.Config(app_factory(), **config_kwargs))
        return await _serve(server, logger, process)

    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        # uvicorn captured the SIGINT, shut down cleanly, and re-raised it out of
        # `serve()` (see the module docstring). `uvicorn.run()` swallows it; so do we,
        # or a dev's Ctrl-C prints a traceback over an otherwise clean shutdown.
        logger.info("%s: interrupted — shutdown complete", process)
        return 0
