"""The process-lifecycle wrapper every long-running worker entrypoint runs under.

`run_daemon` is `asyncio.run` plus SIGTERM: as PID 1 the kernel drops an unhandled one, so
the entrypoints' `finally` cleanup never ran. The handler cancels the main task; a SIGTERM
arriving while a cancel is already in flight is logged and IGNORED, because re-cancelling
aborts that cleanup. SIGINT is untouched, and a clean SIGTERM shutdown exits 0
(see docs/cleanup/WORKLOG.md #14).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable

__all__ = ["run_daemon"]


async def _supervise(
    main: Callable[[], Awaitable[int]],
    logger: logging.Logger,
    process: str,
) -> int:
    """Run *main* as a task with SIGTERM wired to cancel it; return its exit code."""
    loop = asyncio.get_running_loop()
    main_task = asyncio.ensure_future(main())
    # Read by the handler AND by the CancelledError branch below, which has to tell
    # "we asked for this" (clean shutdown, exit 0) apart from "something else cancelled
    # us" (SIGINT — must propagate).
    terminating = False

    def _on_sigterm() -> None:
        nonlocal terminating
        if terminating:
            # See the module docstring: re-cancelling here would abort the cleanup
            # that is already running. SIGKILL is the escalation path.
            logger.warning(
                "%s: SIGTERM received again while already shutting down — IGNORED "
                "(cleanup is in progress; a second cancel would abort it). Send "
                "SIGKILL to force.",
                process,
            )
            return
        if main_task.cancelling() > 0:
            # SIGINT-then-SIGTERM. Someone else's cancel is already propagating (Ctrl-C
            # under `asyncio.Runner` cancels the task awaiting `main_task`, which
            # cancels `main_task`), so the cleanup is mid-flight for the same reason as
            # above and a second cancel would abort it just the same. `terminating`
            # stays FALSE on purpose: the CancelledError branch must still see this as
            # an outside cancellation and re-raise, or Ctrl-C stops being Ctrl-C.
            logger.warning(
                "%s: SIGTERM received while a cancellation is already propagating "
                "(SIGINT/Ctrl-C) — IGNORED (cleanup is in progress; a second cancel "
                "would abort it). Send SIGKILL to force.",
                process,
            )
            return
        terminating = True
        logger.info(
            "%s: SIGTERM received — cancelling the main task so shutdown cleanup runs",
            process,
        )
        main_task.cancel()

    handler_installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        handler_installed = True
    except (NotImplementedError, RuntimeError):
        # No loop-level signal support (Windows, or a non-main thread). Say so rather
        # than pretending: on such a platform the PID-1 drop is back, and silence is
        # what made the original defect invisible for as long as it was.
        logger.warning(
            "%s: could not install a SIGTERM handler on this event loop — shutdown "
            "cleanup will NOT run on SIGTERM",
            process,
        )

    try:
        return await main_task
    except asyncio.CancelledError:
        if terminating:
            logger.info("%s: shutdown complete (cleanup ran)", process)
            return 0
        # Cancelled by something other than our handler — SIGINT under
        # `asyncio.Runner` is the real case. The cancel has ALREADY propagated into
        # `main_task` (it was this task's `_fut_waiter`), so its `finally` blocks have
        # run; all that is left is to not swallow the cancellation.
        raise
    finally:
        if handler_installed:
            # The loop is about to close, but an un-removed handler keeps a reference
            # to a dead task's `cancel` — and leaving it installed breaks any test that
            # runs two daemons in one process.
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signal.SIGTERM)


def run_daemon(
    main: Callable[[], Awaitable[int]],
    *,
    logger: logging.Logger,
    process: str,
) -> int:
    """`asyncio.run(main())`, but SIGTERM cancels *main* instead of being dropped.

    *main* is called here rather than awaited by the caller, so the task is created INSIDE the
    loop that will run it. Returns *main*'s exit code, or 0 when a SIGTERM shutdown completed;
    every other exception — including the KeyboardInterrupt following a SIGINT — propagates.
    """
    return asyncio.run(_supervise(main, logger, process))
