"""Layer-2 integration — the S9 corpus-landing writer for GLOBAL_KNOWLEDGE against a
LIVE neo4j + the real D71 embedding mock (UI Slice 2 §1.1). The proofs only real infra
can give:

  1. land an approved global_knowledge candidate into REAL neo4j via the real writer,
     then RECALL it through `Neo4jVectorIndex.recall(kind="knowledge")` — proving the
     flagship "approve → retrievable" action works end-to-end;
  2. a SECOND land of the same canonical_key MERGEs the same `:KnowledgeChunk` (no
     duplicate) — the deterministic `kn::`-id idempotency;
  3. a retraction (`update_status(status="retired")`) removes it from recall (the
     §1.1 row 5 knowledge status filter), while a hand-authored seed chunk survives.

Skip-guarded on NEO4J_TEST_URI + EMBEDDING_TEST_URL (mirrors
`test_learning_corpus_landing_live.py`); `uv run pytest` with no live stack stays green:

    docker compose -f docker-compose.integration.yml up -d --wait neo4j embedding-api
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_learning_knowledge_landing_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.promotion.landing import CorpusLandingWriter, landing_id
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import (
    apply_schema,
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
_KEY = "sha256:live-landing-kn"
_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_SEED_KNOWLEDGE_ID = "kn-overtime-multiplier"  # a hand-authored (created_by=seed) chunk
_STATEMENT = (
    "Overtime is paid at 1.5 times the base hourly rate for hours worked beyond 40 "
    "in a single work week."
)


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


def _knowledge_candidate(*, canonical_key: str = _KEY) -> CandidateEnvelope:
    """An approved global_knowledge candidate, stamped with a dedup `canonical_key` so
    the `kn::` landing id is deterministic."""
    return CandidateEnvelope.from_doc(
        {
            "candidate_id": "candidate::hash-live-kn::0",
            "type": "global_knowledge",
            "status": "validated",
            "payload": {
                "statement": _STATEMENT,
                "scope": "payroll policy",
                "related_terms": ["overtime", "base rate"],
                "knowledge_type": "business_rule",
            },
            "provenance": {
                "source_session": "sess-live-kn",
                "source_trace": "trace-live-kn",
                "evidence_ref": ["evidence::sess-live-kn::e1"],
                "extractor_rationale": "a durable payroll rule",
            },
            "entity_scan": {"result": "pass", "hits": []},
            "confidence": 0.95,
            "proposed_action": "new",
            "depends_on": [],
            "content_hash": "hash-live-kn",
            "dedup": {
                "canonical_key": canonical_key,
                "matched_id": None,
                "similarity": 1.0,
                "action": "increment",
                "layer": "hard",
            },
        }
    )


@pytest.fixture(scope="module")
def clean_schema() -> bool:
    """Wipe the ephemeral l2 neo4j + provision the corpus schema once per module (the
    writer runs `ensure_schema=False` — it is a writer, not a provisioner, §3.3)."""

    async def _prep() -> None:
        driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
        try:
            async with driver.session() as session:
                # DESTRUCTIVE: wipes ALL nodes. Point NEO4J_TEST_URI ONLY at the
                # ephemeral l2 compose neo4j, NEVER a real/shared instance.
                await session.run("MATCH (n) DETACH DELETE n")
            await apply_schema(driver, dimension=768)
        finally:
            await driver.close()

    asyncio.run(_prep())
    return True


async def test_land_then_recall_knowledge_end_to_end(clean_schema: bool) -> None:
    """Land an approved global_knowledge candidate via the REAL writer, then recall it
    back through `Neo4jVectorIndex.recall(kind="knowledge")` — it became RECALLABLE."""
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    env = _knowledge_candidate()
    try:
        writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
        await writer.land(env)
    finally:
        await driver.close()

    index = Neo4jVectorIndex(url=_uri(), auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0)
    try:
        query_vector = (await _embedder().embed([_STATEMENT]))[0]
        got = await index.recall(query_vector=query_vector, kind="knowledge", k=10)
    finally:
        await index.close()

    landed = next((c for c in got if c.id == landing_id(env)), None)
    assert landed is not None, "the landed knowledge chunk must be recallable"
    assert landed.id == f"kn::{_KEY}"  # the kn:: prefix — disjoint from bp::
    assert _STATEMENT in landed.text


async def test_second_land_merges_no_duplicate(clean_schema: bool) -> None:
    """A second land of the SAME canonical_key MERGEs the same node — exactly one
    `:KnowledgeChunk` with the deterministic `kn::` id."""
    env = _knowledge_candidate()
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
        await writer.land(env)
        await writer.land(env)  # re-land — MERGE by id, never a dupe
        async with driver.session() as session:
            result = await session.run(
                "MATCH (k:KnowledgeChunk {id: $id}) "
                "RETURN count(k) AS c, collect(DISTINCT k.created_by) AS created_by, "
                "collect(DISTINCT k.source_candidate_id) AS source",
                {"id": landing_id(env)},
            )
            row = await result.single()
    finally:
        await driver.close()

    assert row is not None
    assert row["c"] == 1  # one node, not two
    assert row["created_by"] == ["learning"]
    assert row["source"] == [env.candidate_id]


async def test_retract_excludes_knowledge_from_recall_seed_survives() -> None:
    """Land a knowledge chunk alongside the hand-authored seed corpus, then RETRACT it
    (`status=retired`): the landed chunk is NO LONGER recalled (the §1.1 row 5 filter),
    while the hand-authored seed chunk stays recallable throughout (byte-compat)."""
    env = _knowledge_candidate(canonical_key="sha256:live-retract-kn")
    landed_id = landing_id(env)

    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        async with driver.session() as session:
            # DESTRUCTIVE: ephemeral l2 neo4j only (see clean_schema note).
            await session.run("MATCH (n) DETACH DELETE n")
        await apply_schema(driver, dimension=768)
        # Catalog-owned :Table/:Column graph first so load_corpus' MERGE→MATCH :USES
        # edges bind to real catalog nodes (no spurious edge-drift warnings).
        await load_catalog_graph(driver, load_catalog_export())
        blueprints, knowledge = load_seed_fixtures(_CORPUS_DIR)
        await load_corpus(driver, _embedder(), blueprints, knowledge, model_id=_MODEL)
        writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
        await writer.land(env)
    finally:
        await driver.close()

    query_vector = (await _embedder().embed([_STATEMENT]))[0]

    index = Neo4jVectorIndex(url=_uri(), auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0)
    try:
        before = {
            c.id for c in await index.recall(query_vector=query_vector, kind="knowledge", k=30)
        }
    finally:
        await index.close()
    assert landed_id in before, "the landed knowledge chunk must be recallable before retract"
    assert _SEED_KNOWLEDGE_ID in before, "the hand-authored seed chunk must be recallable"

    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
        stamped = await writer.update_status(env, status="retired", drift_status="clean")
    finally:
        await driver.close()
    assert stamped is True  # the landed node was found + stamped

    index = Neo4jVectorIndex(url=_uri(), auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0)
    try:
        after = {
            c.id for c in await index.recall(query_vector=query_vector, kind="knowledge", k=30)
        }
    finally:
        await index.close()
    assert landed_id not in after, "a retracted knowledge chunk must NOT be recallable"
    assert _SEED_KNOWLEDGE_ID in after, "the hand-authored seed chunk stays recallable throughout"
