"""The process-lifecycle wrapper every long-running worker entrypoint runs under.

`run_daemon` is `asyncio.run` plus the ONE thing `asyncio.run` does not do: answer
SIGTERM.

Why that matters is entirely about PID 1. The Helm chart launches each worker as
`command: [python, scripts/run_X.py]`, so Python IS the container's PID 1 — and PID 1
is special: the kernel delivers a signal to it ONLY if it has installed a handler.
An unhandled SIGTERM against any other process terminates it; against PID 1 it is
silently DROPPED. `asyncio.run` installs a SIGINT handler (3.11+ `asyncio.Runner`) and
NO SIGTERM handler, so every one of these workers ignored the signal Kubernetes uses
to stop things. The consequence was not a slow shutdown, it was a WRONG one: the
kubelet sends SIGTERM, the pod sits out the full `terminationGracePeriodSeconds`
doing more work, and is then SIGKILLed mid-extraction / mid-stream-drain — which skips
every `finally` in the entrypoint (the neo4j driver close in `run_learning_consumer`,
the hydrator's `close()`). Those `finally` blocks were already written and correct;
nothing ever ran them.

So the fix is not new cleanup code, it is DELIVERING THE CANCEL that makes the
existing cleanup run:

    SIGTERM -> handler -> main_task.cancel() -> CancelledError raised at the current
    await -> the entrypoint's `finally` blocks run -> shutdown

`loop.add_signal_handler` (not `signal.signal`) is what makes this safe: the callback
runs on the event loop rather than re-entrantly on whatever bytecode was executing,
so `task.cancel()` is called from a context where cancellation means what it says.

**A SIGTERM THAT ARRIVES WHILE A CANCELLATION IS ALREADY IN FLIGHT IS LOGGED AND
IGNORED.** It does not re-cancel and does not escalate. Re-cancelling a task that is
already inside its `finally` would raise CancelledError out of
`await neo4j_driver.close()` — i.e. it would abort exactly the cleanup this module
exists to guarantee, turning an impatient operator's second Ctrl-C-equivalent into the
very failure mode the first signal fixed. Forcing the issue is SIGKILL's job, and
Kubernetes already sends it at the end of the grace period. The ignored signal is
logged at WARNING so an operator watching the logs learns that the shutdown is in
progress rather than wondering whether the signal landed.

"Already in flight" is TWO conditions, not one. The obvious one is a second SIGTERM
(`terminating` is set). The other is the INTERLEAVE: Ctrl-C first, SIGTERM second —
common when a dev impatiently follows a Ctrl-C with a `kill`, and reachable in a pod
whenever anything signals the container before the kubelet does. A SIGINT-initiated
cancel is not ours and leaves `terminating` False, so the flag alone would let the
SIGTERM handler fire a SECOND cancel into a task mid-cleanup — the exact abort the
second-SIGTERM rule prevents. `main_task.cancelling() > 0` is the direct question
("has anyone already requested a cancel of this task?") and answers both cases. On
that path `terminating` is deliberately LEFT UNSET, because the CancelledError branch
below reads it to decide between "we asked for this, exit 0" and "someone else
cancelled us, re-raise": setting it would convert the interactive operator's
KeyboardInterrupt into a silent exit 0.

**SIGINT is deliberately untouched.** `asyncio.Runner` already cancels the main task
on Ctrl-C and re-raises KeyboardInterrupt; adding a handler here would change an
interactive behaviour that was never broken. Note that a SIGINT cancels the task
running `_supervise`, whose `_fut_waiter` IS the main task — so the cancel propagates
inward, the entrypoint's `finally` blocks still run, and the KeyboardInterrupt still
surfaces. That path is asserted in the tests.

**Exit code 0 on a SIGTERM shutdown.** A worker that received the stop signal, cancelled
cleanly and ran its cleanup has SUCCEEDED at stopping; 143 (128+SIGTERM) would paint
every ordinary rollout as a container error in `kubectl get pod` last-state and in any
alerting built on it. Entrypoints keep their own non-zero codes for real misprovision
(the scheduler returns 1 when its buckets are absent), so a bad deploy is still
distinguishable from a normal stop.

Deliberately NOT in here: anything the entrypoints differ on. No settings, no logging
setup (that is `learning/entrypoint.configure_daemon_process` for the learning daemons,
and the hydrator's own `basicConfig`), no shutdown timeout. A cleanup that hangs is a
bug in the cleanup, and capping it here would hide it behind a wrapper timeout while
still losing the work.
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

    *main* is the entrypoint's zero-argument async `_main` (called here, not awaited by
    the caller, so the task is created INSIDE the loop that will run it). *logger* is
    the calling entrypoint's module logger, so the shutdown lines are attributed to the
    script an operator is reading; *process* names the worker in those lines.

    Returns *main*'s exit code, or 0 when a SIGTERM shutdown completed. Every exception
    other than the cancellation we caused — including the KeyboardInterrupt that follows
    a SIGINT — propagates unchanged.
    """
    return asyncio.run(_supervise(main, logger, process))
