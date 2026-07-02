"""Layer-2 integration — the retrieval pipeline end-to-end against the REAL
D71 embedding + reranker mocks (design §5 Layer-2), no neo4j (Slice 1).

Wires the live `HttpEmbeddingClient` (18003) + `HttpRerankerClient` (18004) into
`RetrievalPipeline` over a `FakeVectorIndex` seeded with realistic blueprint
cards / knowledge chunks. Proves the whole embed→recall→rerank→cut path against
real weights before the neo4j brick (Slice 2) lands.

Skip-guarded on BOTH EMBEDDING_TEST_URL and RERANKER_TEST_URL (mirrors
`test_embedding_api.py` / `test_reranker_api.py`) so `uv run pytest` with no live
stack stays fully green. Run with the Layer-2 stack up:

    docker compose -f docker-compose.integration.yml up -d --wait embedding-api reranker-api
    EMBEDDING_TEST_URL=http://localhost:18003/embed \
    RERANKER_TEST_URL=http://localhost:18004/rerank \
        uv run pytest tests/integration/test_retrieval_pipeline_live.py -v
"""

from __future__ import annotations

import os

import pytest

from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.model.reranker_client import HttpRerankerClient
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex

pytestmark = pytest.mark.skipif(
    not (os.environ.get("EMBEDDING_TEST_URL") and os.environ.get("RERANKER_TEST_URL")),
    reason="Requires live embedding+reranker APIs (set EMBEDDING_TEST_URL + RERANKER_TEST_URL).",
)


def _embedder() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"],
        api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
        model="all-mpnet-base-v2",
        timeout_seconds=30.0,
    )


def _reranker() -> HttpRerankerClient:
    return HttpRerankerClient(
        url=os.environ["RERANKER_TEST_URL"],
        api_key=os.environ.get("RERANKER_TEST_API_KEY", ""),
        model="ms-marco-MiniLM-L-6-v2",
        timeout_seconds=30.0,
    )


_RELEVANT_INTENT = "Total overtime pay by department for a given pay period"
_IRRELEVANT_INTENT = "Office building locations and their postal addresses"
_QUESTION = "how much overtime did the sales department pay out last month"


def _blueprint(id: str, intent: str) -> Candidate:
    return Candidate(
        id=id,
        kind="blueprint",
        text=intent,
        uses=frozenset({"w.payroll.overtime"}),
        payload={"intent": intent, "slots_summary": "department, pay_period"},
    )


def _pipeline(index: FakeVectorIndex, *, rerank: bool) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedding_client=_embedder(),
        reranker=_reranker() if rerank else None,
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
        reranker_model="ms-marco-MiniLM-L-6-v2",
    )


async def test_semantic_ordering_relevant_blueprint_outranks_irrelevant() -> None:
    # Seed the fake index with the REAL embeddings of each intent, so recall is
    # genuinely semantic; then the full real embed→recall→rerank pipeline must
    # surface the overtime blueprint first for an overtime question.
    embedder = _embedder()
    vecs = await embedder.embed([_RELEVANT_INTENT, _IRRELEVANT_INTENT])
    index = FakeVectorIndex(
        [
            (_blueprint("bp-overtime", _RELEVANT_INTENT), vecs[0]),
            (_blueprint("bp-locations", _IRRELEVANT_INTENT), vecs[1]),
        ]
    )

    ctx = await _pipeline(index, rerank=True).retrieve(
        question=_QUESTION, column_scope=frozenset(), user_id=None
    )

    assert ctx.reranked is True
    assert [c.id for c in ctx.thin_cards][0] == "bp-overtime"


async def test_rerank_changes_an_adversarial_recall_order() -> None:
    # Force an ADVERSARIAL recall order: give the irrelevant blueprint a stored
    # vector equal to the query embedding (recall similarity 1.0 → ranked first),
    # and the relevant one the negated query vector (similarity -1.0 → last). The
    # real cross-encoder rerank on the intent TEXT must then flip the order.
    embedder = _embedder()
    query_vec = (await embedder.embed([_QUESTION]))[0]
    negated = [-x for x in query_vec]
    index = FakeVectorIndex(
        [
            (_blueprint("bp-overtime", _RELEVANT_INTENT), negated),
            (_blueprint("bp-locations", _IRRELEVANT_INTENT), query_vec),
        ]
    )

    recall_order = await _pipeline(index, rerank=False).retrieve(
        question=_QUESTION, column_scope=frozenset(), user_id=None
    )
    reranked = await _pipeline(index, rerank=True).retrieve(
        question=_QUESTION, column_scope=frozenset(), user_id=None
    )

    recall_ids = [c.id for c in recall_order.thin_cards]
    reranked_ids = [c.id for c in reranked.thin_cards]

    assert recall_ids[0] == "bp-locations"  # adversarial: irrelevant first on pure recall
    assert reranked_ids[0] == "bp-overtime"  # rerank fixed it
    assert recall_ids != reranked_ids  # rerank actually changed the order
    assert reranked.reranked is True


async def test_knowledge_hits_reranked_semantically() -> None:
    embedder = _embedder()
    relevant = "Overtime is paid at 1.5x the base hourly rate for hours beyond 40/week."
    irrelevant = "The company cafeteria serves lunch between 11am and 2pm on weekdays."
    vecs = await embedder.embed([relevant, irrelevant])
    index = FakeVectorIndex(
        [
            (Candidate(id="kn-ot", kind="knowledge", text=relevant, uses=None, payload={}), vecs[0]),
            (Candidate(id="kn-caf", kind="knowledge", text=irrelevant, uses=None, payload={}), vecs[1]),
        ]
    )

    ctx = await _pipeline(index, rerank=True).retrieve(
        question="how is overtime pay calculated", column_scope=frozenset(), user_id=None
    )
    assert [h.id for h in ctx.knowledge_hits][0] == "kn-ot"
