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
import sys
from pathlib import Path

from data_agent.catalog.loader import build_sqlglot_schema_from_catalog
from data_agent.learning.audit.couchbase_audit_store import CouchbaseAuditStore
from data_agent.learning.candidate.couchbase_candidate_store import CouchbaseCandidateStore
from data_agent.learning.config import LearningSettings
from data_agent.learning.dedup.couchbase_corpus import CouchbaseBlueprintCorpus
from data_agent.learning.entrypoint import configure_daemon_process
from data_agent.learning.extractor.grounding import (
    known_rule_ids_from_catalog,
    rule_index_from_catalog,
)
from data_agent.learning.factory import build_learning_consumer
from data_agent.learning.redis_queue import RedisStreamsLearningQueue
from data_agent.learning.user.config import UserKnowledgeStoreConfig
from data_agent.learning.user.couchbase_user_store import CouchbaseUserKnowledgeStore
from data_agent.runtime.config import get_runtime_settings
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.model.openai_client import build_openai_model_client
from data_agent.runtime.session.couchbase_store import CouchbaseSessionStore

# `_catalog` is a sibling module under `scripts/`. Put this script's own directory
# on `sys.path` so the import resolves BOTH when run as `python scripts/x.py` AND
# when the file is loaded by path (importlib `spec_from_file_location`, e.g. tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _catalog import catalog_dict, catalog_fixture_path  # noqa: E402

_logger = logging.getLogger(__name__)


async def _main() -> int:
    runtime_settings = get_runtime_settings()
    learning_settings = LearningSettings()
    user_config = UserKnowledgeStoreConfig()
    tracer = configure_daemon_process("consumer", learning_settings, _logger)

    store = CouchbaseSessionStore(runtime_settings)
    queue = RedisStreamsLearningQueue.from_settings(learning_settings)

    # The neo4j driver is owned by THIS function: created below only when the
    # prior-art index is wired, and closed in the `finally` that wraps EVERYTHING
    # from here on — not just `run_forever`. A `finally` around the run loop alone
    # would leak the pool whenever composition itself raised (a `LearningWiringError`,
    # a bad catalog snapshot), which is precisely the startup path most likely to
    # fail. Mirrors how the scheduler entrypoint owns and closes its driver.
    neo4j_driver = None
    try:
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
            # Semantic catalog source (D75 Wave 1b — `databaseSchemaDocs/` is gone). This
            # consumer is an OFFLINE Redis/Couchbase worker: it drains the learning stream
            # and never touches the MCP read plane, so it holds NO per-request MCP JWT and
            # cannot authenticate the live `GET /catalog/export`. It therefore reads the
            # FROZEN committed export snapshot (`tests/fixtures/catalog_export.json`, the
            # SAME payload the MCP serves) resolved by `catalog_fixture_file()`. To refresh
            # against the LIVE catalog without a redeploy, dump the MCP's `/catalog/export`
            # body to a file and point `CATALOG_FIXTURE_PATH` at it. Loaded ONCE at startup
            # and reused for the whole run; the extractor needs the sqlglot schema (D69)
            # plus the `rules[*].id` grounding (the `rule` role, MEDIUM-2 fix).
            catalog = catalog_dict(runtime_settings)
            _logger.info(
                "semantic catalog loaded from frozen export snapshot %s "
                "(%d tables; set CATALOG_FIXTURE_PATH to a live /catalog/export dump to refresh)",
                catalog_fixture_path(runtime_settings),
                len(catalog),
            )
            model_client = build_openai_model_client(
                api_key=learning_settings.learning_extractor_api_key,
                model=learning_settings.learning_extractor_model,
                base_url=learning_settings.learning_extractor_base_url,
            )
            # The coverage judge's own client (plan §3b), built ONLY when
            # `LEARNING_JUDGE_MODEL` names a different model. The judge sees a bounded
            # brief plus five summary cards where the extractor ships the whole session,
            # so a smaller model is the economically right answer — but a second client
            # on the SAME model would be two connection pools for one behaviour, so the
            # default is to reuse the extractor's. Same API key and base URL: the judge
            # is not a second provider, it is a second model on the same one.
            judge_model_client = None
            if (
                learning_settings.learning_judge_model
                and learning_settings.learning_judge_model
                != learning_settings.learning_extractor_model
            ):
                judge_model_client = build_openai_model_client(
                    api_key=learning_settings.learning_extractor_api_key,
                    model=learning_settings.learning_judge_model,
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
            # PriorArt Slice 2 — the CROSS-TIER prior-art index. This process had no neo4j
            # driver at all before now (only the scheduler and the inbox service did), which
            # is precisely why the dedup stage could see nothing but the `learning_corpus`
            # bucket it seeds itself.
            #
            # FAIL-OPEN, not all-or-nothing (unlike the extraction unit above): a missing or
            # unreachable graph must NEVER stop the loop draining its queue. Absent, the
            # factory logs loudly and dedup falls back to exactly today's behaviour. The
            # driver's timeouts bound the "unreachable host" degrade so a down neo4j raises
            # `PriorArtUnavailable` inside the budget instead of hanging a candidate.
            #
            # The SAME `embedder` instance is reused deliberately: the prior-art query text
            # must be embedded with the model the corpus was built with, and
            # `RuntimeSettings.embedding_model` is the single source of truth for that (it is
            # what `build_hydrator` passes as both the client's `model` and the vector
            # index's `expected_model`). Building a second client, or configuring a second
            # model id, would produce a 100% `model_matched=False` rate indistinguishable
            # from a genuine corpus skew.
            prior_art = None
            if (
                runtime_settings.neo4j_url
                and runtime_settings.neo4j_username
                and runtime_settings.neo4j_password
                and embedder is not None
            ):
                from neo4j import AsyncGraphDatabase

                from data_agent.learning.priorart.neo4j_index import Neo4jPriorArtIndex

                neo4j_driver = AsyncGraphDatabase.driver(
                    runtime_settings.neo4j_url,
                    auth=(runtime_settings.neo4j_username, runtime_settings.neo4j_password),
                    connection_timeout=runtime_settings.neo4j_timeout_seconds,
                    connection_acquisition_timeout=runtime_settings.neo4j_timeout_seconds,
                    max_transaction_retry_time=runtime_settings.neo4j_timeout_seconds,
                )
                prior_art = Neo4jPriorArtIndex(
                    driver=neo4j_driver,
                    embedding_client=embedder,
                    expected_model=runtime_settings.embedding_model,
                )
            else:
                _logger.warning(
                    "prior-art index NOT wired (neo4j_url=%s creds=%s embedder=%s) — the "
                    "dedup stage will see ONLY the learning_corpus bucket it seeds itself, "
                    "so the MCP canon and the landed learning tier are invisible and "
                    "already-owned blueprints will be re-proposed as new",
                    bool(runtime_settings.neo4j_url),
                    bool(runtime_settings.neo4j_username and runtime_settings.neo4j_password),
                    embedder is not None,
                )
            _logger.info(
                "extractor model configured — building the full write-router pipeline "
                "(audit=%s candidates=%s corpus=%s user=%s embedder=%s prior_art=%s); any "
                "missing durable collaborator FAILS FAST at composition (prior_art is "
                "fail-open and excluded from that rule)",
                audit_store is not None,
                candidate_store is not None,
                blueprint_corpus is not None,
                user_store is not None,
                embedder is not None,
                prior_art is not None,
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
                judge_model_client=judge_model_client,
                audit_store=audit_store,
                candidate_store=candidate_store,
                blueprint_corpus=blueprint_corpus,
                prior_art=prior_art,
                user_store=user_store,
                catalog_schema=build_sqlglot_schema_from_catalog(catalog),
                embedder=embedder,
                known_rules=known_rule_ids_from_catalog(catalog),
                # The SAME catalog, projected a second way: which table each rule is
                # declared on. Read only when a plan cites a rule id that does not
                # exist, to decide whether the decline can name the id that was meant
                # instead of dying on a label (`extractor/rule_match.py`).
                rule_index=rule_index_from_catalog(catalog),
            )

        _logger.info(
            "learning consumer starting (group=%s, consumer=%s, batch=%s)",
            learning_settings.learning_consumer_group,
            learning_settings.learning_consumer_name,
            learning_settings.learning_batch_size,
        )
        await consumer.run_forever(sleep=asyncio.sleep)
    finally:
        if neo4j_driver is not None:
            await neo4j_driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
