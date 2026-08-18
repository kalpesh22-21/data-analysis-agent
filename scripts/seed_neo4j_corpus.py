#!/usr/bin/env python
"""seed_neo4j_corpus — CLI wrapper that seeds the neo4j retrieval corpus.

Reads the hand-authored fixtures under `tests/fixtures/corpus/`, builds a real
`HttpEmbeddingClient` + an async neo4j driver from environment variables, and runs
`corpus_loader.load_corpus` (schema DDL + embed + idempotent MERGE upsert +
`:Column`/`:Table` edges + write-time model parity). The write logic stays in
`corpus_loader` rather than a raw Cypher dump so the seed's node shape cannot drift
from what `Neo4jVectorIndex` reads back.

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

SAFETY — by DEFAULT this does an ADDITIVE corpus upsert (`gc=False`). `--reconcile`
(alias `--gc`) enables the DESTRUCTIVE fixture-based reconcile, which DELETES every
`source='mcp'` node ABSENT from these fixtures. Do NOT `--reconcile` against a DB seeded
from the LIVE MCP export — the fixtures are a small dev subset, so the sweep would wipe
the real canon. The reconcile is `source='mcp'`-scoped and can never touch a
`source='learning'` staging node either way.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from neo4j import AsyncGraphDatabase

from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.corpus_loader import (
    corpus_content_sha,
    load_catalog_graph,
    load_corpus,
    load_seed_fixtures,
)

# `_catalog` is a sibling module under `scripts/`. Put this script's own directory
# on `sys.path` so the import resolves BOTH when run as `python scripts/x.py` AND
# when the file is loaded by path (importlib `spec_from_file_location`, e.g. tests).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _catalog import catalog_export, catalog_handle  # noqa: E402

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "corpus"
_logger = logging.getLogger(__name__)


def _try_load_catalog() -> CatalogHandle | None:
    """Build the CatalogHandle for the D94 Part-3 seed-time skew warning, softly.

    A dev-time early warning, never a seed precondition: if the frozen catalog-export
    snapshot is absent or unreadable, log a note and return `None` so `load_corpus`
    still proceeds with the skew check skipped."""
    try:
        return catalog_handle()
    except Exception as exc:  # noqa: BLE001 - soft dev-time aid, never blocks the seed
        _logger.warning(
            "catalog unavailable (%s) — skipping the D94 seed-time skew check; "
            "the corpus load proceeds unchanged",
            exc,
        )
        return None


async def _try_seed_catalog_graph(driver: object, database: str) -> None:
    """Seed the enriched `:Table`/`:Column` catalog graph from the frozen export
    snapshot, softly. Mirrors `_try_load_catalog`: if the export is absent/unreadable
    the corpus seed still proceeds (the `:USES` edges are then skipped as drift and a
    warning is logged by `load_corpus`), so this never blocks a manual seed."""
    try:
        export = catalog_export()
    except Exception as exc:  # noqa: BLE001 - soft dev-time aid, never blocks the seed
        _logger.warning(
            "catalog export unavailable (%s) — skipping catalog-graph hydration; "
            "load_corpus will report any blueprint→column edge drift",
            exc,
        )
        return
    # The seed script is the EXPLICIT reconcile/maintenance path: gc=True runs the
    # full reconcile (dropped-column GC + the GC-referenced drift signal). The online
    # B1 self-heal (app.py) is upsert-only (gc=False) to avoid the rolling-deploy
    # mutual-GC race.
    graph_report = await load_catalog_graph(driver, export, database=database, gc=True)  # type: ignore[arg-type]
    _logger.info("catalog graph seeded: %s", graph_report)


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Seed the neo4j retrieval corpus.")
    parser.add_argument(
        "--reconcile",
        "--gc",
        dest="reconcile",
        action="store_true",
        help=(
            "DESTRUCTIVE: run the fixture-based corpus GC, deleting every source='mcp' "
            "node absent from these fixtures. Default off (additive upsert only). Never "
            "use against a DB seeded from the live MCP export."
        ),
    )
    args = parser.parse_args()

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
        # Hydrate the enriched, self-healing `:Table`/`:Column` catalog graph FIRST
        # (same path the runtime B1 self-heal uses), so `load_corpus`'s MERGE→MATCH
        # `:USES` edges bind to real catalog nodes. Best-effort: a missing/unreadable
        # export snapshot logs a note and proceeds (edges are then skipped as drift).
        await _try_seed_catalog_graph(driver, database)
        # Governed corpus (Phase 2): the corpus load is ADDITIVE by default (gc=False)
        # — an upsert that never deletes. `--reconcile` opts into the DESTRUCTIVE
        # fixture-based GC (gc=True), which DELETES any `source='mcp'` node absent from
        # these fixtures (a full reconcile). The GC is `source='mcp'`-scoped, so it can
        # NEVER touch a learning-staging node either way. The content-derived
        # `corpus_sha` is stable per fixture content, so a re-seed of unchanged fixtures
        # is a B1 no-op and (under --reconcile) any edit flips the stamp to reap stale
        # mcp nodes. See the module docstring for the "never against live-MCP" warning.
        corpus_sha = corpus_content_sha(blueprints, knowledge)
        report = await load_corpus(
            driver,
            embedder,
            blueprints,
            knowledge,
            model_id=model_id,
            database=database,
            catalog=catalog,
            corpus_sha=corpus_sha,
            gc=args.reconcile,
        )
    finally:
        await driver.close()

    mode = "RECONCILE (gc)" if args.reconcile else "additive"
    print(
        f"Seeded neo4j corpus [{mode}] (model={report.model_id}): "
        f"{report.blueprints_written} blueprints, {report.knowledge_written} knowledge, "
        f"{report.columns_referenced} columns, {report.tables_referenced} tables."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
