#!/usr/bin/env python
"""run_learning_sweeper — the learning-loop SWEEPER entrypoint (D96 §g).

Composes a `LearningSweeper` from real infra (Couchbase session store + Redis Streams
queue) and runs the periodic idle-detection loop. The D58c kill-switch is read FRESH at
the top of every cycle, so flipping `LEARNING_ENABLED` halts enqueue without a restart.

Environment: `RuntimeSettings` (Couchbase: COUCHBASE_*) + `LearningSettings`
(LEARNING_*). Traced to the Phoenix `learning-loop` project — the startup log says
whether tracing is actually ON (an empty `OTLP_ENDPOINT` yields a NO-OP provider and
ZERO spans).

Usage:
    uv run python scripts/run_learning_sweeper.py            # loop forever
    uv run python scripts/run_learning_sweeper.py --once     # ONE sweep, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from data_agent.daemon import run_daemon
from data_agent.learning.config import get_learning_settings
from data_agent.learning.entrypoint import configure_daemon_process
from data_agent.learning.redis_queue import RedisStreamsLearningQueue
from data_agent.learning.sweeper import LearningSweeper
from data_agent.runtime.config import get_runtime_settings
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

_logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the learning-loop sweeper.")
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run exactly ONE sweep cycle and exit (for controlled testing: enqueue a "
            "known set of idle sessions, then inspect the stream/statuses without a "
            "second cycle racing the inspection). Default: loop every "
            "LEARNING_SWEEP_INTERVAL_SECONDS."
        ),
    )
    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    runtime_settings = get_runtime_settings()
    learning_settings = get_learning_settings()
    tracer = configure_daemon_process("sweeper", learning_settings, _logger)

    store = CouchbaseSessionStore(runtime_settings)
    queue = RedisStreamsLearningQueue.from_settings(learning_settings)
    sweeper = LearningSweeper(store, queue, learning_settings, tracer=tracer)

    if args.once:
        _logger.info(
            "learning sweeper single cycle (--once, idle_threshold=%ss)",
            learning_settings.learning_idle_threshold_seconds,
        )
        # `run_forever` normally does this once before its first cycle; a single
        # cycle needs the same guarantee that the group exists (the sweeper XADDs
        # to a stream whose consumer group must exist for the entries to be
        # deliverable).
        await queue.ensure_group()
        result = await sweeper.run_once()
        _logger.info(
            "learning sweep complete: scanned=%d claimed=%d enqueued=%d disabled=%s",
            result.scanned, result.claimed, result.enqueued, result.disabled,
        )
        return 0

    _logger.info(
        "learning sweeper starting (interval=%ss, idle_threshold=%ss)",
        learning_settings.learning_sweep_interval_seconds,
        learning_settings.learning_idle_threshold_seconds,
    )
    await sweeper.run_forever(sleep=asyncio.sleep)
    return 0


if __name__ == "__main__":
    # `run_daemon`, not `asyncio.run`: PID 1 drops an unhandled SIGTERM, so a rollout
    # would wait out the grace period and SIGKILL this loop mid-cycle — a sweep that has
    # CLAIMED sessions but not yet enqueued them. See `data_agent/daemon.py`.
    raise SystemExit(run_daemon(_main, logger=_logger, process="learning sweeper"))
