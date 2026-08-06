"""Layer-2 integration — the independent, enriched, self-healing catalog graph
(`load_catalog_graph`) against a LIVE neo4j. Four proofs:

  1. hydrate: `:Table`/`:Column` carry REAL enriched props (not just `key`) + the
     `:OF_TABLE` edge + a stamped `catalog_sha`;
  2. self-heal GC: a re-seed with a mutated export dropping one column removes that
     `:Column` (stale stamp), surviving nodes re-stamp to the new sha;
  3. B1 no-op: a same-sha re-seed returns `skipped=True` and writes nothing;
  4. blueprint drift: `load_corpus` mints NO phantom `:Column` for an uncatalogued
     `uses` key — the `:USES` edge is absent and the drift is logged.

Skip-guarded on NEO4J_TEST_URI (+ EMBEDDING_TEST_URL for proof 4's `load_corpus`
embed). `uv run pytest` with no live stack stays fully green. Run with the stack up:

    docker compose -f docker-compose.integration.yml up -d --wait neo4j embedding-api
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_catalog_graph_live.py -v
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    load_catalog_graph,
    load_corpus,
)
from tests._catalog_fixture import load_catalog_export

pytestmark = pytest.mark.skipif(
    not os.environ.get("NEO4J_TEST_URI"),
    reason="Requires a live neo4j (set NEO4J_TEST_URI).",
)

_MODEL = "all-mpnet-base-v2"
_E = "dbpcm_warehouse.employee"
_STATUS_COL = f"{_E}.EmployeeStatus"
_CODE_COL = f"{_E}.EmployeeCode"


def _uri() -> str:
    return os.environ["NEO4J_TEST_URI"]


def _auth() -> tuple[str, str]:
    return (
        os.environ.get("NEO4J_TEST_USER", "neo4j"),
        os.environ.get("NEO4J_TEST_PASSWORD", "testpassword"),
    )


def _embedder() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"],
        api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
        model=_MODEL,
        timeout_seconds=30.0,
    )


async def _wipe(driver: AsyncGraphDatabase) -> None:
    async with driver.session() as session:
        # DESTRUCTIVE: wipes ALL nodes. Point NEO4J_TEST_URI ONLY at the ephemeral l2
        # compose neo4j, NEVER a real/shared instance.
        await session.run("MATCH (n) DETACH DELETE n")


# --------------------------------------------------------------------------
# Proof 1 — hydrate: enriched props + :OF_TABLE + stamped sha
# --------------------------------------------------------------------------


def test_hydrate_writes_enriched_props_and_stamps_sha() -> None:
    async def _run() -> None:
        driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
        export = load_catalog_export()
        try:
            await _wipe(driver)
            report = await load_catalog_graph(driver, export)
            assert report.skipped is False
            assert report.tables_upserted == len(export["catalog"])
            assert report.columns_upserted > 0

            async with driver.session() as session:
                trow = await (
                    await session.run(
                        "MATCH (t:Table {key: $k}) RETURN t.database AS database, "
                        "t.table AS table, t.grain AS grain, t.grain_verifiable AS gv, "
                        "t.catalog_sha AS sha",
                        k=_E,
                    )
                ).single()
                assert trow["database"] == "dbpcm_warehouse"
                assert trow["table"] == "employee"
                assert "EmployeeCode" in trow["grain"]
                assert trow["gv"] is True  # employee omits the flag ⇒ default True
                assert trow["sha"] == export["catalog_sha"]

                # The column carries real props AND the :OF_TABLE edge to its table.
                crow = await (
                    await session.run(
                        "MATCH (c:Column {key: $k})-[:OF_TABLE]->(t:Table {key: $t}) "
                        "RETURN c.name AS name, c.short_name AS short_name, "
                        "c.type AS type, c.values_json AS vj, c.catalog_sha AS sha",
                        k=_STATUS_COL,
                        t=_E,
                    )
                ).single()
                # `name` mirrors the full key (node identity, captions in Bloom); the
                # bare short name (casing preserved, D70) lives in `short_name`.
                assert crow["name"] == _STATUS_COL
                assert crow["short_name"] == "EmployeeStatus"
                assert crow["type"] == "Nullable(String)"
                assert crow["vj"] is not None  # values map JSON-encoded
                assert crow["sha"] == export["catalog_sha"]
        finally:
            await driver.close()

    asyncio.run(_run())


# --------------------------------------------------------------------------
# Proof 2 — self-heal GC: a dropped column is removed, survivors re-stamp
# --------------------------------------------------------------------------


def test_reseed_gc_removes_dropped_column() -> None:
    async def _run() -> None:
        driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
        export = load_catalog_export()
        try:
            await _wipe(driver)
            await load_catalog_graph(driver, export)

            # Mutate: drop EmployeeStatus from employee + bump the sha (so it's not
            # a no-op). The dropped column keeps the OLD stamp → GC removes it.
            mutated = copy.deepcopy(export)
            del mutated["catalog"][_E]["columns"]["EmployeeStatus"]
            mutated["catalog_sha"] = export["catalog_sha"] + "0"  # still 40+ hex-ish

            report = await load_catalog_graph(driver, mutated)
            assert report.skipped is False
            assert report.columns_gc >= 1

            async with driver.session() as session:
                gone = await (
                    await session.run("MATCH (c:Column {key: $k}) RETURN c", k=_STATUS_COL)
                ).single()
                assert gone is None  # the dropped column was GC'd

                # A surviving column re-stamped to the NEW sha.
                survivor = await (
                    await session.run(
                        "MATCH (c:Column {key: $k}) RETURN c.catalog_sha AS sha",
                        k=_CODE_COL,
                    )
                ).single()
                assert survivor["sha"] == mutated["catalog_sha"]
        finally:
            await driver.close()

    asyncio.run(_run())


# --------------------------------------------------------------------------
# Proof 3 — B1 no-op: a same-sha re-seed skips without writing
# --------------------------------------------------------------------------


def test_same_sha_reseed_is_a_noop() -> None:
    async def _run() -> None:
        driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
        export = load_catalog_export()
        try:
            await _wipe(driver)
            first = await load_catalog_graph(driver, export)
            assert first.skipped is False

            second = await load_catalog_graph(driver, export)
            assert second.skipped is True
            assert second.tables_upserted == 0
            assert second.columns_upserted == 0
            assert second.catalog_sha == export["catalog_sha"]
        finally:
            await driver.close()

    asyncio.run(_run())


# --------------------------------------------------------------------------
# Proof 4 — blueprint drift: no phantom :Column, :USES absent, drift logged
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("EMBEDDING_TEST_URL"),
    reason="load_corpus embeds the blueprint intent (set EMBEDDING_TEST_URL).",
)
def test_uncatalogued_use_key_mints_no_phantom_column(
    caplog: pytest.LogCaptureFixture,
) -> None:
    phantom_col = f"{_E}.PhantomColumnDoesNotExist"
    phantom_bp = BlueprintSeed(
        id="bp-catalog-graph-phantom-test",
        intent="a blueprint referencing an uncatalogued column",
        slots_summary="",
        uses=[phantom_col],
    )

    async def _run() -> None:
        driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
        try:
            await _wipe(driver)
            # Seed the catalog graph — the phantom column is NOT part of it.
            await load_catalog_graph(driver, load_catalog_export())
            await load_corpus(driver, _embedder(), [phantom_bp], [], model_id=_MODEL)

            async with driver.session() as session:
                # No phantom :Column node was minted (MERGE→MATCH, catalog-owned).
                minted = await (
                    await session.run("MATCH (c:Column {key: $k}) RETURN c", k=phantom_col)
                ).single()
                assert minted is None

                # The blueprint has NO :USES edge (its only use key is uncatalogued).
                edges = await (
                    await session.run(
                        "MATCH (b:Blueprint {id: $id}) "
                        "OPTIONAL MATCH (b)-[u:USES]->() RETURN count(u) AS edges",
                        id=phantom_bp.id,
                    )
                ).single()
                assert edges["edges"] == 0
        finally:
            await driver.close()

    with caplog.at_level(logging.WARNING, logger="data_agent.runtime.retrieval.corpus_loader"):
        asyncio.run(_run())

    # The drift was surfaced as a structured warning naming the blueprint + key.
    assert any(
        "edge drift" in rec.getMessage() and phantom_bp.id in rec.getMessage()
        for rec in caplog.records
    )
