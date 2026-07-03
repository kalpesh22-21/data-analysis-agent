#!/usr/bin/env python
"""seed_neo4j_corpus — CLI wrapper that seeds the neo4j retrieval corpus.

Reads the hand-authored fixtures under `tests/fixtures/corpus/`, builds a real
`HttpEmbeddingClient` + an async neo4j driver from environment variables, and
runs `corpus_loader.load_corpus` (schema DDL + embed + idempotent MERGE upsert +
`:Column`/`:Table` edges + write-time model parity). Keeping the write logic in
`corpus_loader` (not a raw Cypher dump) means the seed's node shape cannot drift
from what `Neo4jVectorIndex` reads back — the same code writes what tests read
(neo4j-corpus-design §3.1).

Environment:
    NEO4J_URI            bolt URL (default bolt://localhost:7687)
    NEO4J_USER           default neo4j
    NEO4J_PASSWORD       default testpassword
    NEO4J_DATABASE       default neo4j
    EMBEDDING_URL        embedding API /embed endpoint (required)
    EMBEDDING_API_KEY    optional bearer key
    EMBEDDING_MODEL      model id stamped on every node (default all-mpnet-base-v2)

Usage:
    EMBEDDING_URL=http://localhost:18003/embed uv run python scripts/seed_neo4j_corpus.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from neo4j import AsyncGraphDatabase

from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.provenance.catalog_handle import (
    CatalogHandle,
    load_catalog_handle,
)
from data_agent.runtime.retrieval.corpus_loader import load_corpus, load_seed_fixtures

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "corpus"
_logger = logging.getLogger(__name__)


def _try_load_catalog() -> CatalogHandle | None:
    """Build the CatalogHandle for the D94 Part-3 seed-time skew warning, softly.

    The check is a dev-time early warning, never a seed precondition: if the
    schema dir is absent or `load_catalog_handle()` raises, log a note and return
    `None` so `load_corpus` still proceeds (with the skew check skipped)."""
    try:
        return load_catalog_handle()
    except Exception as exc:  # noqa: BLE001 - soft dev-time aid, never blocks the seed
        _logger.warning(
            "catalog unavailable (%s) — skipping the D94 seed-time skew check; "
            "the corpus load proceeds unchanged",
            exc,
        )
        return None


async def _main() -> int:
    embedding_url = os.environ.get("EMBEDDING_URL")
    if not embedding_url:
        print("EMBEDDING_URL is required (the /embed endpoint).", file=sys.stderr)
        return 2

    model_id = os.environ.get("EMBEDDING_MODEL", "all-mpnet-base-v2")
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "testpassword")
    database = os.environ.get("NEO4J_DATABASE", "neo4j")

    blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
    embedder = HttpEmbeddingClient(
        url=embedding_url,
        api_key=os.environ.get("EMBEDDING_API_KEY", ""),
        model=model_id,
        timeout_seconds=30.0,
    )
    catalog = _try_load_catalog()
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        report = await load_corpus(
            driver,
            embedder,
            blueprints,
            knowledge,
            model_id=model_id,
            database=database,
            catalog=catalog,
        )
    finally:
        await driver.close()

    print(
        f"Seeded neo4j corpus (model={report.model_id}): "
        f"{report.blueprints_written} blueprints, {report.knowledge_written} knowledge, "
        f"{report.columns_written} columns, {report.tables_written} tables."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
