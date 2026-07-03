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
from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.consumer import LearningConsumer
from data_agent.learning.extractor import ExtractorConfig, LearningExtractor
from data_agent.learning.extractor.grounding import load_known_rule_ids
from data_agent.learning.observability import configure_learning_tracing, get_learning_tracer
from data_agent.learning.redis_queue import RedisStreamsLearningQueue
from data_agent.runtime.config import get_runtime_settings
from data_agent.runtime.model.openai_client import build_openai_model_client
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

    # HIGH-2: (extractor, audit, candidates) are ONE unit. Real extraction writes
    # entity-bearing evidence to `learning_audit` AND persists candidates to
    # `learning_candidates`; running it with a NON-durable candidate store (or a
    # missing audit store) would silently lose candidates on restart / leave
    # dangling evidence_refs. So the extractor is enabled ONLY when the extractor
    # model AND both durable Couchbase stores are configured. Otherwise we FALL
    # BACK to the S2 `would_extract` stub (no extraction, NO audit writes) — the
    # safe choice for a partially-provisioned deploy — and log it loudly.
    extractor_ready = bool(learning_settings.learning_extractor_api_key)
    audit_ready = bool(
        learning_settings.learning_audit_username and learning_settings.learning_audit_password
    )
    candidates_ready = bool(
        learning_settings.learning_candidates_username
        and learning_settings.learning_candidates_password
    )

    if extractor_ready and audit_ready and candidates_ready:
        audit = CouchbaseAuditStore(learning_settings)
        candidates = CouchbaseCandidateStore(learning_settings)
        model_client = build_openai_model_client(
            api_key=learning_settings.learning_extractor_api_key,
            model=learning_settings.learning_extractor_model,
            base_url=learning_settings.learning_extractor_base_url,
        )
        extractor = LearningExtractor(
            model_client,
            config=ExtractorConfig(
                max_retries=learning_settings.learning_extractor_max_retries,
                # MEDIUM-2: ground the `rule` role in the semantic catalog's rule
                # ids (else every rule-role plan declines missing_rule). Full
                # RAG-over-corpus dedup grounding is a documented later-slice
                # deferral (see extractor/grounding.py).
                known_rules=load_known_rule_ids(),
            ),
        )
        _logger.info("S3 extractor ENABLED (durable audit + candidate stores wired)")
    else:
        if extractor_ready:
            _logger.warning(
                "extractor model configured but audit_ready=%s candidates_ready=%s — "
                "FALLING BACK to the would-extract stub (no extraction, no evidence "
                "writes) to avoid lost candidates / dangling evidence_refs. Provision "
                "learning_audit AND learning_candidates to enable S3 extraction.",
                audit_ready, candidates_ready,
            )
        else:
            _logger.info("extractor model unconfigured — KEEP path uses the would-extract stub")
        audit = None
        candidates = None
        extractor = None

    consumer = LearningConsumer(
        store, queue, learning_settings,
        tracer=tracer, audit=audit, extractor=extractor, candidates=candidates,
    )

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
