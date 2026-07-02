"""Layer-2 integration — the REAL custom embedding API (D71), the live
`HttpEmbeddingClient` against the dockerized mock (all-mpnet-base-v2, 768-dim).

Skip-guarded on `EMBEDDING_TEST_URL` (mirrors `test_mcp_scope_live.py`'s
`MCP_TEST_URL` pattern) so `uv run pytest` with no live embedding service stays
fully green. Run with the Layer-2 stack up:

    docker compose -f docker-compose.integration.yml up -d --wait embedding-api
    EMBEDDING_TEST_URL=http://localhost:18003/embed uv run pytest \
        tests/integration/test_embedding_api.py -v

Assertions are deterministic contract properties (vector count, dimensionality,
finiteness) plus a semantic sanity check that a synonym pair scores closer than
an unrelated pair — using the runtime's OWN cosine (`composite/ranking.py`).
"""

from __future__ import annotations

import os

import pytest

from data_agent.runtime.composite.ranking import cosine
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("EMBEDDING_TEST_URL"),
    reason="Requires a live custom embedding API (set EMBEDDING_TEST_URL).",
)

_EXPECTED_DIM = 768


def _client() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"],
        api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
        model="all-mpnet-base-v2",
        timeout_seconds=30.0,
    )


async def test_live_embed_returns_one_768_dim_finite_vector_per_text() -> None:
    texts = ["paid time off", "overtime", "annual salary"]
    vectors = await _client().embed(texts)

    assert len(vectors) == len(texts)
    for vec in vectors:
        assert len(vec) == _EXPECTED_DIM
        assert all(isinstance(x, float) for x in vec)


async def test_live_embed_empty_input_returns_empty_list() -> None:
    # Short-circuits with no HTTP call, but assert against the live client too.
    assert await _client().embed([]) == []


async def test_live_embed_semantic_synonym_beats_unrelated() -> None:
    # "PTO - Paid Time Off" is a synonym of "paid time off"; "OT - Overtime" is
    # a different concept. A real sentence embedder must place the synonym
    # strictly closer under the runtime's own cosine.
    concept, synonym, unrelated = (
        "paid time off",
        "PTO - Paid Time Off",
        "OT - Overtime",
    )
    vectors = await _client().embed([concept, synonym, unrelated])
    concept_vec, synonym_vec, unrelated_vec = vectors

    sim_synonym = cosine(concept_vec, synonym_vec)
    sim_unrelated = cosine(concept_vec, unrelated_vec)
    assert sim_synonym > sim_unrelated, (sim_synonym, sim_unrelated)
