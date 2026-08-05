"""Layer-2 integration — the governed-corpus trust gate + reconcile against a LIVE
neo4j + the real D71 embedding mock (Phase 2). Three proofs:

  1. RECALL serves ONLY `source='mcp'`: a `source='learning'` clone AND a clone with
     NO `source` property are BOTH excluded from blueprint + knowledge recall, while
     the `source='mcp'` canon node is returned.
  2. `getBlueprint` (keyed fetch) is source-gated: a learning clone by id returns None.
  3. GC (`gc=True`) reaps a STALE `source='mcp'` node but NEVER a `source='learning'`
     node (a full reconcile at a new corpus_sha leaves the staging tier intact).

Skip-guarded on NEO4J_TEST_URI + EMBEDDING_TEST_URL (the seed embeds through the real
endpoint). `uv run pytest` with no live stack stays fully green. Run with the stack up:

    docker compose -f docker-compose.integration.yml up -d --wait neo4j embedding-api
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_governed_corpus_live.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import (
    load_catalog_graph,
    load_corpus,
    load_seed_fixtures,
)
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex
from tests._catalog_fixture import load_catalog_export

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_OVERTIME_ID = "bp-overtime-by-department"
_KNOWLEDGE_ID = "kn-overtime-multiplier"
_OVERTIME_QUESTION = "how much overtime did the sales department pay out last pay period"
_OVERTIME_TEXT_QUESTION = "how is overtime pay calculated"


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


def _index() -> Neo4jVectorIndex:
    return Neo4jVectorIndex(
        url=_uri(), auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0
    )


# Clone the seeded mcp canon node into a learning-tier + a no-source sibling, copying
# the embedding + embedding_model + status/drift so ONLY `source` differs — isolating
# the trust gate as the sole reason the clones are excluded from recall. CRITICAL: the
# clone's unique `id` is set AT CREATE time (never `SET x = properties(b)` which would
# momentarily copy the source node's unique id and trip the blueprint_id/knowledge_id
# uniqueness constraint before the override). Only the recall-relevant properties are
# copied; the no-source node simply never has `source` set.
_CLONE_BLUEPRINTS = """
MATCH (b:Blueprint {id: $id})
CREATE (learn:Blueprint {
    id: $id + '::learning', source: 'learning',
    intent: b.intent, slots_summary: b.slots_summary,
    intent_embedding: b.intent_embedding, embedding_model: b.embedding_model,
    uses: b.uses, status: b.status, drift_status: b.drift_status
})
CREATE (nosrc:Blueprint {
    id: $id + '::nosource',
    intent: b.intent, slots_summary: b.slots_summary,
    intent_embedding: b.intent_embedding, embedding_model: b.embedding_model,
    uses: b.uses, status: b.status, drift_status: b.drift_status
})
"""

_CLONE_KNOWLEDGE = """
MATCH (k:KnowledgeChunk {id: $id})
CREATE (learn:KnowledgeChunk {
    id: $id + '::learning', source: 'learning',
    text: k.text, title: k.title,
    text_embedding: k.text_embedding, embedding_model: k.embedding_model,
    doc_id: k.doc_id, status: k.status
})
CREATE (nosrc:KnowledgeChunk {
    id: $id + '::nosource',
    text: k.text, title: k.title,
    text_embedding: k.text_embedding, embedding_model: k.embedding_model,
    doc_id: k.doc_id, status: k.status
})
"""


async def _seed(driver, corpus_sha: str) -> None:
    async with driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")  # DESTRUCTIVE — ephemeral l2 only
    await load_catalog_graph(driver, load_catalog_export())
    blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
    await load_corpus(
        driver, _embedder(), blueprints, knowledge, model_id=_MODEL, corpus_sha=corpus_sha
    )


# --------------------------------------------------------------------------
# Proof 1 — recall serves ONLY source='mcp' (learning + no-source excluded)
# --------------------------------------------------------------------------


async def test_recall_excludes_learning_and_no_source_nodes() -> None:
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        await _seed(driver, corpus_sha="sha-a")
        async with driver.session() as session:
            await session.run(_CLONE_BLUEPRINTS, id=_OVERTIME_ID)
            await session.run(_CLONE_KNOWLEDGE, id=_KNOWLEDGE_ID)
    finally:
        await driver.close()

    index = _index()
    try:
        bp_vec = (await _embedder().embed([_OVERTIME_QUESTION]))[0]
        bp_hits = await index.recall(query_vector=bp_vec, kind="blueprint", k=30)
        kn_vec = (await _embedder().embed([_OVERTIME_TEXT_QUESTION]))[0]
        kn_hits = await index.recall(query_vector=kn_vec, kind="knowledge", k=30)
        # getBlueprint by id is source-gated too (defense-in-depth): both a learning
        # clone AND a sourceless clone resolve to None; only the mcp canon returns.
        learn_detail = await index.get_blueprint(f"{_OVERTIME_ID}::learning")
        nosrc_detail = await index.get_blueprint(f"{_OVERTIME_ID}::nosource")
        mcp_detail = await index.get_blueprint(_OVERTIME_ID)
    finally:
        await index.close()

    bp_ids = {c.id for c in bp_hits}
    assert _OVERTIME_ID in bp_ids  # the mcp canon is recalled
    assert f"{_OVERTIME_ID}::learning" not in bp_ids  # learning tier excluded
    assert f"{_OVERTIME_ID}::nosource" not in bp_ids  # absent-source excluded (fail-closed)

    kn_ids = {c.id for c in kn_hits}
    assert _KNOWLEDGE_ID in kn_ids
    assert f"{_KNOWLEDGE_ID}::learning" not in kn_ids
    assert f"{_KNOWLEDGE_ID}::nosource" not in kn_ids

    assert learn_detail is None  # keyed fetch of a learning node → None
    assert nosrc_detail is None  # keyed fetch of a sourceless node → None (fail-closed)
    assert mcp_detail is not None and mcp_detail.id == _OVERTIME_ID


# --------------------------------------------------------------------------
# Proof 3 — GC reaps a stale source='mcp' node but NEVER a learning node
# --------------------------------------------------------------------------


async def test_gc_reconcile_never_deletes_a_learning_node() -> None:
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        # Seed the canon at sha-a, then add a learning node + a soon-to-be-stale mcp node.
        await _seed(driver, corpus_sha="sha-a")
        async with driver.session() as session:
            await session.run(_CLONE_BLUEPRINTS, id=_OVERTIME_ID)
            # A stale mcp node carrying the OLD sha, which the next reconcile drops.
            # id set at CREATE time (never copy the source node's unique id).
            await session.run(
                "MATCH (b:Blueprint {id: $id}) CREATE (s:Blueprint {"
                "id: 'bp-stale-mcp', source: 'mcp', corpus_sha: 'sha-a', "
                "intent: b.intent, embedding_model: b.embedding_model})",
                id=_OVERTIME_ID,
            )

        # A full reconcile at a NEW sha: re-seed the SAME fixtures (so the canon nodes
        # re-stamp to sha-b) with gc=True. The stale mcp node (sha-a) is reaped; the
        # learning node survives (the GC is source='mcp'-scoped).
        blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
        await load_corpus(
            driver,
            _embedder(),
            blueprints,
            knowledge,
            model_id=_MODEL,
            corpus_sha="sha-b",
            gc=True,
        )

        async with driver.session() as session:
            rows = await (
                await session.run(
                    "MATCH (b:Blueprint) RETURN collect(b.id) AS ids"
                )
            ).single()
    finally:
        await driver.close()

    ids = set(rows["ids"])
    # SAFETY: the learning node survived the source='mcp'-scoped GC.
    assert f"{_OVERTIME_ID}::learning" in ids
    # The stale mcp node was reaped by the reconcile.
    assert "bp-stale-mcp" not in ids
    # The canon node is still present (re-stamped to the new sha).
    assert _OVERTIME_ID in ids
