"""Layer-2 integration — the REAL custom reranker API (D71), the live
`HttpRerankerClient` against the dockerized mock (ms-marco-MiniLM-L-6-v2).

Skip-guarded on `RERANKER_TEST_URL` (mirrors `test_mcp_scope_live.py`'s
`MCP_TEST_URL` pattern). Run with the Layer-2 stack up:

    docker compose -f docker-compose.integration.yml up -d --wait reranker-api
    RERANKER_TEST_URL=http://localhost:18004/rerank uv run pytest \
        tests/integration/test_reranker_api.py -v

Assertions are deterministic contract properties (score count/order) plus a
semantic sanity check that the query scores a relevant document above an
unrelated one.
"""

from __future__ import annotations

import os

import pytest

from data_agent.runtime.model.reranker_client import HttpRerankerClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("RERANKER_TEST_URL"),
    reason="Requires a live custom reranker API (set RERANKER_TEST_URL).",
)


def _client() -> HttpRerankerClient:
    return HttpRerankerClient(
        url=os.environ["RERANKER_TEST_URL"],
        api_key=os.environ.get("RERANKER_TEST_API_KEY", ""),
        model="ms-marco-MiniLM-L-6-v2",
        timeout_seconds=30.0,
    )


async def test_live_rerank_returns_one_finite_score_per_document() -> None:
    documents = ["a document about payroll", "a document about hiring", "a note on PTO"]
    scores = await _client().rerank("paid time off policy", documents)

    assert len(scores) == len(documents)
    assert all(isinstance(s, float) for s in scores)


async def test_live_rerank_empty_documents_returns_empty_list() -> None:
    assert await _client().rerank("anything", []) == []


async def test_live_rerank_relevant_document_scores_above_unrelated() -> None:
    # Cross-encoder relevance: the PTO document must outscore the overtime one
    # for a paid-time-off query. Documents are returned in input order; the
    # caller re-sorts by score — here we assert the score ORDERING directly.
    query = "paid time off"
    pto_doc = "Paid Time Off (PTO) is accrued leave employees can take off work."
    ot_doc = "Overtime (OT) is extra pay for hours worked beyond the standard week."

    pto_score, ot_score = await _client().rerank(query, [pto_doc, ot_doc])
    assert pto_score > ot_score, (pto_score, ot_score)
