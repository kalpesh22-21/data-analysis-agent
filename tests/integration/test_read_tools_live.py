"""Layer-2 integration — the model-facing read tools (read-tools-design §7)
against a LIVE neo4j + the real D71 embedding/reranker mocks. The proofs:

  1. searchBlueprints semantically matches — the overtime blueprint ranks above
     the headcount blueprint for an overtime query (real embed + neo4j + rerank);
  2. a scope-narrowed JWT DROPS an out-of-scope blueprint from searchBlueprints
     (the transitive-USES pre-filter over real stored USES);
  3. getBlueprint round-trips by id (taken from a searchBlueprints result),
     returning the stored projection with byte-exact `uses`;
  4. getBlueprint under a scope that misses one column returns {found: false}
     (the non-oracle, over real USES);
  5. searchKnowledge returns a seeded chunk and is UNAFFECTED by scope;
  6. an unreachable neo4j degrades every tool cleanly (empty / not-found).

Skip-guarded on NEO4J_TEST_URI + EMBEDDING_TEST_URL; the semantic-order proof
additionally needs RERANKER_TEST_URL. `uv run pytest` with no live stack stays
fully green. Run with the stack up:

    docker compose -f docker-compose.integration.yml up -d --wait \
        neo4j embedding-api reranker-api
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
    RERANKER_TEST_URL=http://localhost:18004/rerank \
        uv run pytest tests/integration/test_read_tools_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.model.reranker_client import HttpRerankerClient
from data_agent.runtime.retrieval.corpus_loader import (
    load_catalog_graph,
    load_corpus,
    load_seed_fixtures,
)
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.tools import (
    GetBlueprintTool,
    SearchBlueprintsTool,
    SearchKnowledgeTool,
)
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex
from tests._catalog_fixture import load_catalog_export

pytestmark = pytest.mark.skipif(
    not (os.environ.get("NEO4J_TEST_URI") and os.environ.get("EMBEDDING_TEST_URL")),
    reason="Requires a live neo4j + embedding API (set NEO4J_TEST_URI + EMBEDDING_TEST_URL).",
)

_MODEL = "all-mpnet-base-v2"
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_OVERTIME_ID = "bp-overtime-by-department"
_HEADCOUNT_ID = "bp-active-headcount-by-department"
_OVERTIME_QUESTION = "how much overtime did the sales department pay out last pay period"
_OVERTIME_USES = frozenset(
    {
        "dbpcm_warehouse.payroll.EmployeeCode",
        "dbpcm_warehouse.payroll.RegisterType",
        "dbpcm_warehouse.payroll.Amount",
        "dbpcm_warehouse.payroll.PayPeriodEndDate",
        "dbpcm_warehouse.employee.EmployeeCode",
        "dbpcm_warehouse.employee.Department",
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


def _reranker() -> HttpRerankerClient | None:
    url = os.environ.get("RERANKER_TEST_URL")
    if not url:
        return None
    return HttpRerankerClient(
        url=url,
        api_key=os.environ.get("RERANKER_TEST_API_KEY", ""),
        model="ms-marco-MiniLM-L-6-v2",
        timeout_seconds=30.0,
    )


def _index(*, url: str | None = None) -> Neo4jVectorIndex:
    return Neo4jVectorIndex(
        url=url or _uri(),
        auth=_auth(),
        expected_model=_MODEL,
        timeout_seconds=15.0,
    )


def _pipeline(index: Neo4jVectorIndex) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=_embedder(),
        reranker=_reranker(),
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
        reranker_model="ms-marco-MiniLM-L-6-v2",
    )


def _creds(scope: frozenset[str]) -> RuntimeCredentials:
    return RuntimeCredentials(session_id="s-l2", jwt="jwt", column_scope=scope)


@pytest.fixture(scope="module")
def seeded_corpus() -> bool:
    async def _seed() -> None:
        driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
        try:
            async with driver.session() as session:
                # DESTRUCTIVE — ephemeral l2 compose neo4j ONLY.
                await session.run("MATCH (n) DETACH DELETE n")
            # Catalog-owned :Table/:Column graph first (load_corpus MATCHes these).
            await load_catalog_graph(driver, load_catalog_export())
            blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
            await load_corpus(driver, _embedder(), blueprints, knowledge, model_id=_MODEL)
        finally:
            await driver.close()

    asyncio.run(_seed())
    return True


# --------------------------------------------------------------------------
# Proof 1 — searchBlueprints semantic match (overtime > headcount)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RERANKER_TEST_URL"),
    reason="The semantic-order proof requires a live reranker (set RERANKER_TEST_URL).",
)
async def test_search_blueprints_ranks_overtime_first(seeded_corpus: bool) -> None:
    index = _index()
    tool = SearchBlueprintsTool(pipeline=_pipeline(index), default_k=5, max_k=20)
    try:
        result = await tool.run({"query": _OVERTIME_QUESTION}, _creds(frozenset()))
    finally:
        await index.close()

    assert result.status == "ok"
    assert result.result_full["degraded"] is False  # real rerank happened
    ids = [b["id"] for b in result.result_full["blueprints"]]
    assert ids  # something matched
    assert ids[0] == _OVERTIME_ID
    if _HEADCOUNT_ID in ids:
        assert ids.index(_OVERTIME_ID) < ids.index(_HEADCOUNT_ID)


# --------------------------------------------------------------------------
# Proof 2 — a scope-narrowed JWT drops an out-of-scope blueprint
# --------------------------------------------------------------------------


async def test_search_blueprints_scope_narrows_out_a_blueprint(seeded_corpus: bool) -> None:
    index = _index()
    tool = SearchBlueprintsTool(pipeline=_pipeline(index), default_k=20, max_k=20)
    # A wide scope (superset of the overtime uses) keeps it; a narrow scope
    # missing one column drops it (uses ⊄ scope).
    narrow = frozenset(_OVERTIME_USES - {"dbpcm_warehouse.payroll.Amount"})
    try:
        wide_result = await tool.run({"query": _OVERTIME_QUESTION}, _creds(_OVERTIME_USES))
        narrow_result = await tool.run({"query": _OVERTIME_QUESTION}, _creds(narrow))
    finally:
        await index.close()

    assert _OVERTIME_ID in [b["id"] for b in wide_result.result_full["blueprints"]]
    assert _OVERTIME_ID not in [b["id"] for b in narrow_result.result_full["blueprints"]]


# --------------------------------------------------------------------------
# Proof 3 & 4 — getBlueprint round-trip + non-oracle under a narrow scope
# --------------------------------------------------------------------------


async def test_get_blueprint_round_trip_and_non_oracle(seeded_corpus: bool) -> None:
    index = _index()
    sb = SearchBlueprintsTool(pipeline=_pipeline(index), default_k=20, max_k=20)
    gb = GetBlueprintTool(vector_index=index)
    try:
        found = await sb.run({"query": _OVERTIME_QUESTION}, _creds(_OVERTIME_USES))
        picked_id = next(
            b["id"] for b in found.result_full["blueprints"] if b["id"] == _OVERTIME_ID
        )
        detail = await gb.run({"id": picked_id}, _creds(_OVERTIME_USES))

        # A scope missing one column → the non-oracle {found: false}, identical
        # to a genuinely absent id.
        narrow = frozenset(_OVERTIME_USES - {"dbpcm_warehouse.payroll.Amount"})
        out_of_scope = await gb.run({"id": _OVERTIME_ID}, _creds(narrow))
        absent = await gb.run({"id": "bp-does-not-exist"}, _creds(_OVERTIME_USES))
    finally:
        await index.close()

    assert detail.result_full["found"] is True
    assert detail.result_full["id"] == _OVERTIME_ID
    assert frozenset(detail.result_full["uses"]) == _OVERTIME_USES  # byte-exact
    assert detail.result_full["status"] == "validated"

    assert out_of_scope.result_full == absent.result_full == {"found": False}


# --------------------------------------------------------------------------
# Proof 5 — searchKnowledge returns a seeded chunk, unaffected by scope
# --------------------------------------------------------------------------


async def test_search_knowledge_returns_chunk_and_ignores_scope(seeded_corpus: bool) -> None:
    index = _index()
    tool = SearchKnowledgeTool(pipeline=_pipeline(index), knowledge_k=5)
    try:
        # A NARROW (non-empty) scope must not filter knowledge (entity-agnostic).
        result = await tool.run(
            {"query": "how is overtime pay calculated"}, _creds(frozenset({"x.y.z"}))
        )
    finally:
        await index.close()

    assert result.status == "ok"
    assert result.result_full["count"] >= 1
    assert any(h["id"] == "kn-overtime-multiplier" for h in result.result_full["knowledge"])


# --------------------------------------------------------------------------
# Proof 6 — unreachable neo4j degrades every tool cleanly
# --------------------------------------------------------------------------


async def test_unreachable_neo4j_degrades_all_tools() -> None:
    index = Neo4jVectorIndex(
        url="bolt://localhost:1",
        auth=("neo4j", "wrong"),
        expected_model=_MODEL,
        timeout_seconds=3.0,
    )
    sb = SearchBlueprintsTool(pipeline=_pipeline(index), default_k=5, max_k=20)
    gb = GetBlueprintTool(vector_index=index)
    sk = SearchKnowledgeTool(pipeline=_pipeline(index), knowledge_k=5)
    try:
        sb_result = await sb.run({"query": _OVERTIME_QUESTION}, _creds(frozenset()))
        gb_result = await gb.run({"id": _OVERTIME_ID}, _creds(frozenset()))
        sk_result = await sk.run({"query": "overtime"}, _creds(frozenset()))
    finally:
        await index.close()

    assert sb_result.status == "ok"
    assert sb_result.result_full["blueprints"] == []
    assert sb_result.result_full["degraded"] is True
    assert gb_result.result_full == {"found": False}
    assert sk_result.status == "ok"
    assert sk_result.result_full["knowledge"] == []
