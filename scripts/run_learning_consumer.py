#!/usr/bin/env python
"""run_learning_consumer — the learning-loop CONSUMER entrypoint (D96 §g).

Composes a `LearningConsumer` from real infra (Couchbase session store + Redis
Streams queue) and runs the blocking consume loop. Slice 1 does NO-OP work
(marks `processing → done` + emits a trace event); the loader/triage/extractor is
Slice 2. The D58c kill-switch is read FRESH each cycle inside `run_once`, so
flipping `LEARNING_ENABLED` stops processing without a restart (in-flight work
simply waits in the stream — no loss).

Environment: `RuntimeSettings` (COUCHBASE_*) + `LearningSettings` (LEARNING_*).
Traced to the Phoenix `learning-loop` project.

Usage:
    uv run python scripts/run_learning_consumer.py
"""

from __future__ import annotations

import asyncio
import logging

from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.observability import configure_learning_tracing, get_learning_tracer
from data_agent.learning.redis_queue import RedisStreamsLearningQueue
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
    # S2: the audit client is composed + injected but DORMANT (no snapshot in the
    # S2 path — §4.3); the loader/triage default to the module functions.
    # MEDIUM-2: only build the Couchbase audit client when the learning_audit RBAC
    # creds are actually provisioned. Constructing it with blank credentials builds a
    # client against an unprovisioned bucket — a boot regression. When unconfigured
    # we pass audit=None; the consumer tolerates that (in-memory dormant store).
    if learning_settings.learning_audit_username and learning_settings.learning_audit_password:
        audit = CouchbaseAuditStore(learning_settings)
    else:
        _logger.info("learning_audit unconfigured — audit client dormant (S2 writes no evidence)")
        audit = None
    consumer = LearningConsumer(store, queue, learning_settings, tracer=tracer, audit=audit)

    _logger.info(
        "learning consumer starting (group=%s, consumer=%s, batch=%s)",
        learning_settings.learning_consumer_group,
        learning_settings.learning_consumer_name,
        learning_settings.learning_batch_size,
    )
    await consumer.run_forever(sleep=asyncio.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
