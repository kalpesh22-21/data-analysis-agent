"""`run_daemon` — the SIGTERM contract the four worker entrypoints depend on.

The defect these pin (ISSUES.md H4): the workers run as PID 1 under Helm
(`command: [python, scripts/run_X.py]`), and PID 1 DROPS a signal it has no handler
for. `asyncio.run` installs SIGINT only, so every SIGTERM was ignored, the pod sat out
its grace period, and the SIGKILL that followed skipped the `finally` blocks that close
the neo4j driver / the hydrator. Nothing about the work loops changed to fix it — only
the delivery of the cancel that makes the ALREADY-WRITTEN cleanup run.

So these tests are about cleanup and cancellation, and they send REAL signals
(`os.kill(os.getpid(), SIGTERM)`) rather than calling the handler directly: "the handler
we registered gets invoked by an actual SIGTERM" is precisely the claim that was false
before, and a direct call would assert around it. The signal is always raised from
INSIDE the daemon coroutine, which is the one place the handler is guaranteed to be
installed already (`_supervise` registers it before the first await of the main task) —
an early kill in a test would take the pytest process with it.

These are SYNC tests on purpose: `run_daemon` owns `asyncio.run`, which cannot be
called from inside a running loop (pytest-asyncio's auto mode would supply one).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import pytest

from data_agent.daemon import _supervise, run_daemon

_logger = logging.getLogger("test.daemon")

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# The long-running worker entrypoints the Helm chart execs as PID 1 (deploy/helm/README.md
# — the `hydrator`, `learning-sweeper`, `learning-consumer` and `learning-scheduler`
# Deployments). The uvicorn workloads (runtime / ui / inbox) are NOT here: uvicorn
# installs its own SIGTERM handler.
_PID1_WORKERS = (
    "run_hydrator.py",
    "run_learning_sweeper.py",
    "run_learning_consumer.py",
    "run_learning_scheduler.py",
)


def _sigterm_self() -> None:
    """Deliver a real SIGTERM to this process."""
    os.kill(os.getpid(), signal.SIGTERM)


# --- the exit-code passthrough (no signal involved) ---------------------------


def test_returns_the_entrypoints_own_exit_code():
    """`run_daemon` is a wrapper, not a policy: the scheduler's non-zero
    "misprovisioned, do not start" code must still reach the shell."""

    async def _main() -> int:
        return 1

    assert run_daemon(_main, logger=_logger, process="test") == 1


def test_an_exception_from_the_entrypoint_propagates():
    """A crash must stay a crash — a wrapper that swallowed it into an exit code
    would hide composition failures (`LearningWiringError`) behind a clean stop."""

    async def _main() -> int:
        raise RuntimeError("composition failed")

    with pytest.raises(RuntimeError, match="composition failed"):
        run_daemon(_main, logger=_logger, process="test")


# --- the SIGTERM path ---------------------------------------------------------


def test_sigterm_cancels_the_main_task_and_runs_its_finally():
    """THE regression test for H4. A worker blocked in its run-forever loop must, on
    SIGTERM, reach its `finally` (the driver close) and exit 0 — not be dropped."""
    cleanup: list[str] = []

    async def _main() -> int:
        try:
            _sigterm_self()
            # Stands in for `consumer.run_forever(...)`: never returns on its own, so
            # the ONLY way out of this coroutine is the cancellation we are testing.
            await asyncio.Event().wait()
            return 0  # unreachable
        finally:
            cleanup.append("driver closed")

    rc = run_daemon(_main, logger=_logger, process="test")

    assert rc == 0, "a clean SIGTERM shutdown is a SUCCESS, not 143"
    assert cleanup == ["driver closed"]


def test_sigterm_cleanup_may_await_and_still_completes():
    """The cleanup that actually matters is `await neo4j_driver.close()` — an AWAIT
    inside `finally`, after the CancelledError. It must be allowed to run to completion,
    so the wrapper must not re-cancel or bail out of the shutdown early."""
    cleanup: list[str] = []

    async def _main() -> int:
        try:
            _sigterm_self()
            await asyncio.Event().wait()
            return 0
        finally:
            cleanup.append("close started")
            await asyncio.sleep(0.01)
            cleanup.append("close finished")

    rc = run_daemon(_main, logger=_logger, process="test")

    assert rc == 0
    assert cleanup == ["close started", "close finished"]


def test_a_second_sigterm_during_shutdown_is_ignored_not_escalated(caplog):
    """Documented double-signal behaviour: the second SIGTERM is LOGGED and DROPPED.

    Re-cancelling would raise CancelledError out of the in-flight `await driver.close()`
    and abort exactly the cleanup the first signal exists to guarantee. Escalation is
    SIGKILL's job (Kubernetes sends it at the end of the grace period), so an impatient
    second signal must not be able to produce the failure the fix removed.
    """
    cleanup: list[str] = []

    async def _main() -> int:
        try:
            _sigterm_self()
            await asyncio.Event().wait()
            return 0
        finally:
            cleanup.append("close started")
            # The impatient operator, while the cleanup is mid-flight. The sleeps
            # bracket it so the loop actually dispatches the signal callback during
            # the shutdown rather than after it.
            await asyncio.sleep(0.01)
            _sigterm_self()
            await asyncio.sleep(0.05)
            cleanup.append("close finished")

    with caplog.at_level(logging.INFO, logger="test.daemon"):
        rc = run_daemon(_main, logger=_logger, process="test")

    assert rc == 0
    assert cleanup == ["close started", "close finished"], "the second signal aborted cleanup"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("SIGTERM received again" in m and "IGNORED" in m for m in warnings), (
        "an ignored signal must SAY it was ignored, or the operator cannot tell it "
        "from a signal that never landed"
    )


def test_two_daemons_in_one_process_do_not_leak_the_handler():
    """The handler is removed on the way out. Left installed, it holds a dead task's
    `cancel` — and the second run's SIGTERM would be answered by the first run's
    closed loop."""

    def _one_run() -> list[str]:
        seen: list[str] = []

        async def _main() -> int:
            try:
                _sigterm_self()
                await asyncio.Event().wait()
                return 0
            finally:
                seen.append("done")

        assert run_daemon(_main, logger=_logger, process="test") == 0
        return seen

    assert _one_run() == ["done"]
    assert _one_run() == ["done"], "the second run's SIGTERM was not answered"


# --- cancellation from anywhere else (SIGINT) must NOT be swallowed ------------


async def test_outside_cancellation_still_propagates_after_cleanup():
    """SIGINT is untouched: `asyncio.Runner` cancels the task running `_supervise`,
    not the main task. Because the main task IS that task's `_fut_waiter`, the cancel
    propagates inward and the entrypoint's `finally` runs — but the CancelledError must
    then keep going, or the KeyboardInterrupt an interactive operator expects would be
    swallowed into a clean exit 0.

    Exercised on `_supervise` directly (an outer cancel is what SIGINT produces);
    `run_daemon`'s own `asyncio.run` cannot be re-entered from an async test.
    """
    cleanup: list[str] = []
    started = asyncio.Event()

    async def _main() -> int:
        try:
            started.set()
            await asyncio.Event().wait()
            return 0
        finally:
            cleanup.append("driver closed")

    task = asyncio.ensure_future(_supervise(_main, _logger, "test"))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup == ["driver closed"]


async def test_a_sigterm_during_a_sigint_cancellation_is_ignored(caplog):
    """The INTERLEAVE: Ctrl-C first, then a SIGTERM while the cleanup is mid-flight.

    The `terminating` flag only knows about cancels WE started, so on this path it is
    False and the SIGTERM handler would happily fire a second `main_task.cancel()` into
    a task that is already inside its `finally` — aborting `await driver.close()`, which
    is the one thing the second-SIGTERM rule exists to prevent. The guard therefore also
    asks `main_task.cancelling()`.

    Both halves are asserted, because the cheap fix (set `terminating` too) buys the
    cleanup back and breaks the other half: the CancelledError must STILL propagate, or
    an outside cancellation silently becomes exit 0 and the operator's KeyboardInterrupt
    disappears.

    Deterministic despite the real signal: the SIGTERM is raised from inside the cleanup
    with `await`s on both sides, so the loop is guaranteed a turn to dispatch its signal
    callback before the cleanup finishes — the same shape as the double-SIGTERM test.
    """
    cleanup: list[str] = []
    started = asyncio.Event()

    async def _main() -> int:
        try:
            started.set()
            await asyncio.Event().wait()
            return 0
        finally:
            cleanup.append("close started")
            await asyncio.sleep(0.01)
            _sigterm_self()  # the impatient `kill` after the Ctrl-C
            await asyncio.sleep(0.05)
            cleanup.append("close finished")

    task = asyncio.ensure_future(_supervise(_main, _logger, "test"))
    await started.wait()
    task.cancel()  # what SIGINT does under `asyncio.Runner`

    with caplog.at_level(logging.INFO, logger="test.daemon"):
        with pytest.raises(asyncio.CancelledError):
            await task

    assert cleanup == ["close started", "close finished"], (
        "the interleaved SIGTERM re-cancelled a task that was already unwinding and "
        "aborted its cleanup"
    )
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("cancellation is already propagating" in m and "IGNORED" in m for m in warnings)


# --- adoption: the workers that run as PID 1 actually use it ------------------


@pytest.mark.parametrize("script", _PID1_WORKERS)
def test_every_pid1_worker_launches_through_run_daemon(script):
    """Checked as SOURCE, not by running the daemons (each needs Couchbase/Redis/neo4j
    to reach its first line). The claim is narrow and structural: the process entry is
    `run_daemon`, not a bare `asyncio.run` — which is precisely the difference between
    a SIGTERM that cancels and a SIGTERM that is dropped. Parameterized over the four
    scripts so a fifth worker Deployment cannot quietly ship without it."""
    source = (_SCRIPTS / script).read_text(encoding="utf-8")
    assert "run_daemon(" in source
    assert "asyncio.run(" not in source, (
        f"{script} still enters through asyncio.run, which installs no SIGTERM handler; "
        "as PID 1 the signal is DROPPED and the shutdown cleanup never runs"
    )
