"""Layer-2 integration — the S9 corpus-landing writer (`CorpusLandingWriter`) against
a LIVE neo4j + the real D71 embedding mock (S9-activation Slice 2, §3). The proofs only
real infra can give:

  1. land a validated candidate into REAL neo4j via the real writer, then RECALL it
     through `Neo4jVectorIndex.recall` — proving it became RECALLABLE end-to-end;
  2. a SECOND land of the same canonical_key MERGEs the same node (no duplicate) —
     the deterministic-id idempotency the whole design rests on.

Skip-guarded on NEO4J_TEST_URI + EMBEDDING_TEST_URL (mirrors
`test_neo4j_vector_index_live.py`); `uv run pytest` with no live stack stays green:

    docker compose -f docker-compose.integration.yml up -d --wait neo4j embedding-api
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
        uv run pytest tests/integration/test_learning_corpus_landing_live.py -v
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.learning.candidate.models import CandidateEnvelope
from data_agent.learning.promotion.landing import CorpusLandingWriter, landing_id
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_loader import (
    apply_schema,
    load_corpus,
    load_seed_fixtures,
)
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_KEY = "sha256:live-landing-bp"
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learning" / "s4_enriched_blueprint.json"
_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_SEED_BLUEPRINT_ID = "bp-overtime-by-department"  # a hand-authored (created_by=seed) node
_INTENT = "total earnings for a department in a given year"
_EXPECTED_USES = frozenset(
    {
        "payroll.payroll_fact.department",
        "payroll.payroll_fact.gross_pay",
        "payroll.payroll_fact.pay_period",
        "payroll.payroll_fact.record_type",
        "payroll.payroll_fact.region",
    }
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


def _validated_candidate(*, canonical_key: str = _KEY) -> CandidateEnvelope:
    """A validated single-blueprint candidate from the frozen S4 fixture, stamped with
    a dedup `canonical_key` so the landing id is deterministic."""
    data = json.loads(_FIXTURE.read_text())
    doc = copy.deepcopy(data["single"]["envelope"])
    doc["status"] = "validated"
    doc["dedup"] = {
        "canonical_key": canonical_key,
        "matched_id": None,
        "similarity": 1.0,
        "action": "increment",
        "layer": "hard",
    }
    return CandidateEnvelope.from_doc(doc)


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
            await apply_schema(driver)
        finally:
            await driver.close()

    asyncio.run(_prep())
    return True


async def test_land_then_recall_end_to_end(clean_schema: bool) -> None:
    """Land a validated candidate via the REAL writer, then recall it back through
    `Neo4jVectorIndex` — it became RECALLABLE, with byte-exact `uses`."""
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    env = _validated_candidate()
    try:
        writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
        await writer.land(env)
    finally:
        await driver.close()

    index = Neo4jVectorIndex(url=_uri(), auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0)
    try:
        query_vector = (await _embedder().embed([_INTENT]))[0]
        got = await index.recall(query_vector=query_vector, kind="blueprint", k=10)
    finally:
        await index.close()

    landed = next((c for c in got if c.id == landing_id(env)), None)
    assert landed is not None, "the landed blueprint must be recallable"
    assert landed.id == f"bp::{_KEY}"
    assert landed.text == _INTENT
    assert landed.uses == _EXPECTED_USES


async def test_second_land_merges_no_duplicate(clean_schema: bool) -> None:
    """A second land of the SAME canonical_key MERGEs the same node — exactly one
    `:Blueprint` with the deterministic id (the idempotency the design rests on)."""
    env = _validated_candidate()
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
        await writer.land(env)
        await writer.land(env)  # re-land — MERGE by id, never a dupe
        async with driver.session() as session:
            result = await session.run(
                "MATCH (b:Blueprint {id: $id}) "
                "RETURN count(b) AS c, collect(DISTINCT b.created_by) AS created_by, "
                "collect(DISTINCT b.source_candidate_id) AS source",
                {"id": landing_id(env)},
            )
            row = await result.single()
    finally:
        await driver.close()

    assert row is not None
    assert row["c"] == 1  # one node, not two
    # Provenance (review S3): the landed node is distinguishable from a hand-authored
    # seed and carries its originating candidate id (incident-response handle).
    assert row["created_by"] == ["learning"]
    assert row["source"] == [env.candidate_id]


# --------------------------------------------------------------------------
# S9-activation Slice 3 — corpus retraction (§8.6): demote write-back removes the
# blueprint from recall while the hand-authored seed corpus stays recallable.
# --------------------------------------------------------------------------


async def test_demote_write_back_excludes_from_recall_seed_survives() -> None:
    """Land a validated blueprint (recallable) alongside the hand-authored seed corpus,
    then DEMOTE it via `update_status` (status=candidate, drift_status=suspect): the
    landed node is NO LONGER recalled (the recall filter excludes it), while the
    hand-authored seed blueprint stays recallable throughout (byte-compat)."""
    env = _validated_candidate(canonical_key="sha256:live-retract-bp")
    landed_id = landing_id(env)

    # Wipe → provision → seed the hand-authored corpus → land the loop blueprint.
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        async with driver.session() as session:
            # DESTRUCTIVE: ephemeral l2 neo4j only (see clean_schema note).
            await session.run("MATCH (n) DETACH DELETE n")
        await apply_schema(driver)
        blueprints, knowledge = load_seed_fixtures(_CORPUS_DIR)
        await load_corpus(driver, _embedder(), blueprints, knowledge, model_id=_MODEL)
        writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
        await writer.land(env)
    finally:
        await driver.close()

    query_vector = (await _embedder().embed([_INTENT]))[0]

    # BEFORE demote: both the landed blueprint AND the seed corpus are recallable.
    index = Neo4jVectorIndex(url=_uri(), auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0)
    try:
        before = {c.id for c in await index.recall(query_vector=query_vector, kind="blueprint", k=30)}
    finally:
        await index.close()
    assert landed_id in before, "the landed blueprint must be recallable before demote"
    assert _SEED_BLUEPRINT_ID in before, "the hand-authored seed must be recallable"

    # Demote via the retraction write-back (the drift-suspect / user-correction edge).
    driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
    try:
        writer = CorpusLandingWriter(driver, _embedder(), model_id=_MODEL)
        stamped = await writer.update_status(env, status="candidate", drift_status="suspect")
    finally:
        await driver.close()
    assert stamped is True  # the landed node was found + stamped

    # AFTER demote: the landed blueprint is filtered out; the seed corpus is unchanged.
    index = Neo4jVectorIndex(url=_uri(), auth=_auth(), expected_model=_MODEL, timeout_seconds=15.0)
    try:
        after = {c.id for c in await index.recall(query_vector=query_vector, kind="blueprint", k=30)}
    finally:
        await index.close()
    assert landed_id not in after, "a demoted blueprint must NOT be recallable"
    assert _SEED_BLUEPRINT_ID in after, "the hand-authored seed stays recallable throughout"
