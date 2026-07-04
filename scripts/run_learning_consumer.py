#!/usr/bin/env python
"""run_learning_consumer — the learning-loop CONSUMER entrypoint (D96 §g).

Composes a `LearningConsumer` from real infra (Couchbase session/audit/candidate/
corpus/user stores + Redis Streams queue) via the Wave-3 composition-root factory
(`build_learning_consumer`) and runs the blocking consume loop. The write-router
pipeline (S4 generalize → S5 leakage → S6 dedup → S8 schema-edit/user-commit →
S7 writer) is assembled by the factory; this entrypoint only constructs the infra
clients and hands them in.

The D58c kill-switch is read FRESH each cycle inside `run_once`, so flipping
`LEARNING_ENABLED` stops processing without a restart (in-flight work simply waits
in the stream — no loss). The plane stays DORMANT until an operator runs this
process AND the kill-switch permits it; there is no request-path import of the
learning plane.

**All-or-nothing gating (S3 precedent, extended).** Real extraction is a UNIT: the
extractor model client + durable audit store + durable candidate store + durable
blueprint corpus + durable per-user store + the catalog. When the extractor model
is unconfigured, the consumer falls back to the S2 `would_extract` stub with an
EMPTY pipeline (the safe partially-provisioned posture). When the extractor model
IS configured, EVERY durable collaborator MUST be provisioned (its RBAC creds
present) — a missing one raises `LearningWiringError` at composition (the factory
enforces this) rather than running a PARTIAL pipeline that would strand candidates
mid-flow. Never a third, partial branch.

Environment: `RuntimeSettings` (COUCHBASE_*, EMBEDDING_*) + `LearningSettings`
(LEARNING_*) + `UserKnowledgeStoreConfig` (USER_KNOWLEDGE_*). Traced to the Phoenix
`learning-loop` project.

Usage:
    uv run python scripts/run_learning_consumer.py
"""

from __future__ import annotations

import asyncio
import logging

from data_agent.catalog.loader import build_sqlglot_schema
from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore
from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
from data_agent.learning.extractor.grounding import load_known_rule_ids
from data_agent.learning.factory import build_learning_consumer
from data_agent.learning.observability import configure_learning_tracing, get_learning_tracer
from data_agent.learning.redis_queue import RedisStreamsLearningQueue
from data_agent.learning.user.config import UserKnowledgeStoreConfig
from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore
from data_agent.runtime.config import get_runtime_settings
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.model.openai_client import build_openai_model_client
from data_agent.runtime.observability.tracing import set_global_tracer_provider
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

_logger = logging.getLogger(__name__)


async def _main() -> int:
    logging.basicConfig(level=logging.INFO)
    runtime_settings = get_runtime_settings()
    learning_settings = LearningSettings()
    user_config = UserKnowledgeStoreConfig()

    provider = configure_learning_tracing(
        otlp_endpoint=learning_settings.otlp_endpoint,
        service_name=learning_settings.learning_service_name,
    )
    set_global_tracer_provider(provider)
    tracer = get_learning_tracer(provider)

    store = CouchbaseSessionStore(runtime_settings)
    queue = RedisStreamsLearningQueue.from_settings(learning_settings)

    # Extraction is a UNIT (the S3 precedent, extended to the full write-router):
    # extractor model + durable audit + durable candidate + durable corpus + durable
    # per-user store + the catalog. When the extractor model is UNCONFIGURED, build
    # the stub consumer (empty pipeline, would-extract fallback). When it IS
    # configured, build every durable collaborator (None where its RBAC creds are
    # absent) and hand them to the factory, which FAILS FAST (`LearningWiringError`)
    # on any missing piece — never a partial pipeline that strands candidates.
    if not learning_settings.learning_extractor_api_key:
        _logger.info(
            "extractor model unconfigured — KEEP path uses the would-extract stub "
            "(empty write-router pipeline)"
        )
        consumer = build_learning_consumer(
            learning_settings, session_store=store, queue=queue, tracer=tracer
        )
    else:
        model_client = build_openai_model_client(
            api_key=learning_settings.learning_extractor_api_key,
            model=learning_settings.learning_extractor_model,
            base_url=learning_settings.learning_extractor_base_url,
        )
        audit_store = (
            CouchbaseAuditStore(learning_settings)
            if learning_settings.learning_audit_username
            and learning_settings.learning_audit_password
            else None
        )
        candidate_store = (
            CouchbaseCandidateStore(learning_settings)
            if learning_settings.learning_candidates_username
            and learning_settings.learning_candidates_password
            else None
        )
        blueprint_corpus = (
            CouchbaseBlueprintCorpus(learning_settings)
            if learning_settings.learning_corpus_username
            and learning_settings.learning_corpus_password
            else None
        )
        user_store = (
            CouchbaseUserKnowledgeStore(user_config)
            if user_config.user_knowledge_username and user_config.user_knowledge_password
            else None
        )
        # The dedup soft-layer embedder: the real HTTP client when the embedding API
        # is configured, else None (the factory defaults to the insert-only embedder
        # → hard-key-only dedup; the soft near-miss layer degrades to insert, D52).
        embedder = (
            HttpEmbeddingClient(
                url=runtime_settings.embedding_api_url,
                api_key=runtime_settings.embedding_api_key,
                model=runtime_settings.embedding_model,
                timeout_seconds=runtime_settings.embedding_timeout_seconds,
                tracer=tracer,
            )
            if runtime_settings.embedding_api_url
            else None
        )
        _logger.info(
            "extractor model configured — building the full write-router pipeline "
            "(audit=%s candidates=%s corpus=%s user=%s embedder=%s); any missing "
            "durable collaborator FAILS FAST at composition",
            audit_store is not None,
            candidate_store is not None,
            blueprint_corpus is not None,
            user_store is not None,
            embedder is not None,
        )
        # `git_client` is left to the factory's null default (opens no real PR, stamps
        # the schema_edit_review marker → routes to human review; real GitHub PR
        # authoring is deferred, D53). `known_rules` grounds the `rule` role in the
        # semantic catalog (MEDIUM-2 fix); `catalog_schema` is the D69 sqlglot catalog.
        consumer = build_learning_consumer(
            learning_settings,
            session_store=store,
            queue=queue,
            tracer=tracer,
            model_client=model_client,
            audit_store=audit_store,
            candidate_store=candidate_store,
            blueprint_corpus=blueprint_corpus,
            user_store=user_store,
            catalog_schema=build_sqlglot_schema(),
            embedder=embedder,
            known_rules=load_known_rule_ids(),
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
