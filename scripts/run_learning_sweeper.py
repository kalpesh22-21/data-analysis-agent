#!/usr/bin/env python
"""run_learning_sweeper — the learning-loop SWEEPER entrypoint (D96 §g).

Composes a `LearningSweeper` from real infra (Couchbase session store + Redis
Streams queue) and runs the periodic idle-detection loop. The D58c kill-switch is
read FRESH at the top of every cycle inside `run_once` (via `learning_enabled()`),
so flipping `LEARNING_ENABLED` halts enqueue without a restart.

Environment: `RuntimeSettings` (Couchbase: COUCHBASE_*) + `LearningSettings`
(LEARNING_*). Traced to the Phoenix `learning-loop` project.

Usage:
    uv run python scripts/run_learning_sweeper.py
"""

from __future__ import annotations

import asyncio
import logging

from data_agent.learning.config import LearningSettings
from data_agent.learning.observability import configure_learning_tracing, get_learning_tracer
from data_agent.learning.redis_queue import RedisStreamsLearningQueue
from data_agent.learning.sweeper import LearningSweeper
from data_agent.runtime.config import get_runtime_settings
from data_agent.runtime.observability.tracing import set_global_tracer_provider
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

_logger = logging.getLogger(__name__)


async def _main() -> int:
    logging.basicConfig(level=logging.INFO)
    runtime_settings = get_runtime_settings()
    learning_settings = LearningSettings()

    provider = configure_learning_tracing(
        otlp_endpoint=learning_settings.otlp_endpoint,
        service_name=learning_settings.learning_service_name,
    )
    set_global_tracer_provider(provider)
    tracer = get_learning_tracer(provider)

    store = CouchbaseSessionStore(runtime_settings)
    queue = RedisStreamsLearningQueue.from_settings(learning_settings)
    sweeper = LearningSweeper(store, queue, learning_settings, tracer=tracer)

    _logger.info(
        "learning sweeper starting (interval=%ss, idle_threshold=%ss)",
        learning_settings.learning_sweep_interval_seconds,
        learning_settings.learning_idle_threshold_seconds,
    )
    await sweeper.run_forever(sleep=asyncio.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
