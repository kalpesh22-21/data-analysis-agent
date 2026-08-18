"""The process-lifecycle wrapper the uvicorn-serving entrypoints run under.

Same policy as `daemon.py` (a clean SIGTERM shutdown exits 0), different mechanism: uvicorn's
`capture_signals` restores the pre-existing SIGTERM handler and re-raises the signal, so at
`SIG_DFL` the process dies inside `serve()` at 143 and nothing after it runs. Ours is chained
in BEFORE `serve()`; installed at IMPORT time it would land inside the captured region and
clobber uvicorn's own graceful shutdown (see docs/cleanup/WORKLOG.md #22).
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

    *app_factory* is handed to uvicorn as a `factory=True` app, so `config.load()` calls it from
    inside `serve()`: in the running loop (load-bearing for the inbox service, whose
    `acouchbase.Cluster` needs one) and inside uvicorn's captured-signal region. Returns 0 for a
    clean stop, or `uvicorn.main.STARTUP_FAILURE` when the lifespan `startup` hook failed; every
    other exception — including anything *app_factory* raises — propagates.
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
