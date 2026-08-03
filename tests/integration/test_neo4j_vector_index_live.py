"""Layer-2 integration — Neo4jVectorIndex against a LIVE neo4j + the real D71
embedding/reranker mocks (neo4j-corpus-design §4.4). The five proofs:

  1. real recall → Candidate mapping with BYTE-EXACT `uses` strings;
  2. scope filtering against real stored USES (the neo4j↔scope-format contract);
  3. full e2e: real embed + real neo4j + real rerank through RetrievalPipeline;
  4. parity guard — a mismatched expected_model excludes everything (→ []);
  5. degrade — an unreachable neo4j → recall [] and the pipeline proceeds empty.

Skip-guarded on NEO4J_TEST_URI + EMBEDDING_TEST_URL (the seed embeds through the
real endpoint); proof 3 additionally needs RERANKER_TEST_URL. `uv run pytest`
with no live stack stays fully green. Run with the stack up:

    docker compose -f docker-compose.integration.yml up -d --wait \
        neo4j embedding-api reranker-api
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_TEST_USER=neo4j \
    NEO4J_TEST_PASSWORD=testpassword \
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
    RERANKER_TEST_URL=http://localhost:18004/rerank \
        uv run pytest tests/integration/test_neo4j_vector_index_live.py -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from neo4j import AsyncGraphDatabase
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.model.reranker_client import HttpRerankerClient
from data_agent.runtime.retrieval.corpus_loader import (
    load_catalog_graph,
    load_corpus,
    load_seed_fixtures,
)
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.scope_filter import filter_blueprints_by_scope
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


def _reranker() -> HttpRerankerClient:
    return HttpRerankerClient(
        url=os.environ["RERANKER_TEST_URL"],
        api_key=os.environ.get("RERANKER_TEST_API_KEY", ""),
        model="ms-marco-MiniLM-L-6-v2",
        timeout_seconds=30.0,
    )


def _index(expected_model: str = _MODEL, *, url: str | None = None) -> Neo4jVectorIndex:
    return Neo4jVectorIndex(
        url=url or _uri(),
        auth=_auth(),
        expected_model=expected_model,
        timeout_seconds=15.0,
    )


@pytest.fixture(scope="module")
def seeded_corpus() -> bool:
    """Seed the live neo4j once per module via the loader under test (§4.2).

    Runs its own event loop (a plain sync fixture) so it never fights the
    per-test event-loop scope; each test then opens its own driver in its own
    loop.
    """

    async def _seed() -> None:
        driver = AsyncGraphDatabase.driver(_uri(), auth=_auth())
        try:
            async with driver.session() as session:
                # DESTRUCTIVE: wipes ALL nodes. Point NEO4J_TEST_URI only at the
                # ephemeral l2 compose neo4j (docker-compose.integration.yml),
                # NEVER at any real/shared instance.
                await session.run("MATCH (n) DETACH DELETE n")
            # Hydrate the catalog-owned :Table/:Column graph FIRST so load_corpus'
            # MERGE→MATCH :USES edges bind to real catalog nodes (columns are no
            # longer minted by the corpus loader).
            await load_catalog_graph(driver, load_catalog_export())
            blueprints, knowledge = load_seed_fixtures(_FIXTURE_DIR)
            await load_corpus(driver, _embedder(), blueprints, knowledge, model_id=_MODEL)
        finally:
            await driver.close()

    asyncio.run(_seed())
    return True


# --------------------------------------------------------------------------
# Proof 1 — recall → Candidate mapping with byte-exact uses
# --------------------------------------------------------------------------


async def test_recall_maps_blueprint_with_byte_exact_uses(seeded_corpus: bool) -> None:
    index = _index()
    try:
        query_vector = (await _embedder().embed([_OVERTIME_QUESTION]))[0]
        got = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
    finally:
        await index.close()

    overtime = next(c for c in got if c.id == _OVERTIME_ID)
    assert overtime.text == "Total overtime pay by department for a given pay period"
    assert overtime.payload["slots_summary"] == "department, pay_period"
    # The load-bearing assertion: uses is a frozenset[str] whose members
    # byte-match the HR-warehouse scope keys.
    assert isinstance(overtime.uses, frozenset)
    assert all(isinstance(u, str) for u in overtime.uses)
    assert overtime.uses == _OVERTIME_USES


# --------------------------------------------------------------------------
# Proof 2 — scope filtering against real stored USES (neo4j↔scope contract)
# --------------------------------------------------------------------------


async def test_scope_filter_uses_round_trip_through_neo4j(seeded_corpus: bool) -> None:
    index = _index()
    try:
        query_vector = (await _embedder().embed([_OVERTIME_QUESTION]))[0]
        got = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
    finally:
        await index.close()

    overtime = next(c for c in got if c.id == _OVERTIME_ID)
    assert overtime.uses is not None

    # A scope MISSING one of the blueprint's columns drops it (uses ⊄ scope).
    narrow_scope = frozenset(overtime.uses - {"dbpcm_warehouse.payroll.Amount"})
    dropped = filter_blueprints_by_scope([overtime], narrow_scope)
    assert dropped == []

    # A scope that is a SUPERSET of uses keeps it — proving the stored strings
    # pass the UNMODIFIED Slice-1 scope filter byte-for-byte.
    wide_scope = frozenset(overtime.uses | {"dbpcm_warehouse.employee.AnnualSalary"})
    kept = filter_blueprints_by_scope([overtime], wide_scope)
    assert [c.id for c in kept] == [_OVERTIME_ID]


# --------------------------------------------------------------------------
# Proof 3 — full e2e: real embed + real neo4j + real rerank through the pipeline
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RERANKER_TEST_URL"),
    reason="The e2e rerank leg requires a live reranker (set RERANKER_TEST_URL).",
)
async def test_end_to_end_pipeline_real_everything(seeded_corpus: bool) -> None:
    index = _index()
    pipeline = RetrievalPipeline(
        embedding_client=_embedder(),
        reranker=_reranker(),
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
        reranker_model="ms-marco-MiniLM-L-6-v2",
    )
    try:
        ctx = await pipeline.retrieve(
            question=_OVERTIME_QUESTION, column_scope=frozenset(), user_id=None
        )
    finally:
        await index.close()

    assert ctx.reranked is True
    card_ids = [c.id for c in ctx.thin_cards]
    assert card_ids  # something was retrieved
    # The overtime blueprint outranks the headcount blueprint for an overtime
    # question (the drop-in changes nothing observable but the store).
    assert card_ids[0] == _OVERTIME_ID
    if _HEADCOUNT_ID in card_ids:
        assert card_ids.index(_OVERTIME_ID) < card_ids.index(_HEADCOUNT_ID)
    # Knowledge recall is genuinely populated too.
    assert any(h.id == "kn-overtime-multiplier" for h in ctx.knowledge_hits)


# --------------------------------------------------------------------------
# Proof 4 — parity guard: a mismatched expected_model excludes everything
# --------------------------------------------------------------------------


async def test_parity_guard_excludes_mismatched_model(seeded_corpus: bool) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    index = _index(expected_model="a-different-model-entirely")
    try:
        query_vector = (await _embedder().embed([_OVERTIME_QUESTION]))[0]
        with tracer.start_as_current_span("recall"):
            got = await index.recall(query_vector=query_vector, kind="blueprint", k=30)
    finally:
        await index.close()

    assert got == []  # mismatched vector space never returns neighbours
    span = exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes.get("retrieval.model_mismatch") is True


# --------------------------------------------------------------------------
# Proof 5 — degrade: unreachable neo4j → recall [] and the pipeline proceeds
# --------------------------------------------------------------------------


async def test_unreachable_neo4j_degrades_to_empty() -> None:
    # A bad bolt port — connection acquisition fails within the timeout; recall
    # must swallow it and return [], never raise (D86).
    index = Neo4jVectorIndex(
        url="bolt://localhost:1",
        auth=("neo4j", "wrong"),
        expected_model=_MODEL,
        timeout_seconds=3.0,
    )
    pipeline = RetrievalPipeline(
        embedding_client=_embedder(),
        reranker=None,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
    )
    try:
        recall_got = await index.recall(query_vector=[0.0] * 768, kind="blueprint", k=30)
        ctx = await pipeline.retrieve(
            question=_OVERTIME_QUESTION, column_scope=frozenset(), user_id=None
        )
    finally:
        await index.close()

    assert recall_got == []
    assert ctx.is_empty()
