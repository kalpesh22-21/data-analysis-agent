"""Layer-2 integration — ResolveValuesComposite end-to-end with the REAL
`HttpEmbeddingClient` (live custom embedding API), a `FakeMCPClient` scripting
the backing `runQuery` rows.

This is the payoff test: it proves the whole D77 ranking path (embed concept +
row texts -> cosine blended with normalized log-freq -> top-K) produces a
SEMANTIC win over pure frequency when a real embedder is in the loop. The rows
are rigged so frequency alone would rank the WRONG value first (OT freq=100 vs.
PTO freq=5); a working semantic ranker must still put PTO first for the concept
"paid time off".

Skip-guarded on `EMBEDDING_TEST_URL` (same pattern as `test_embedding_api.py`).
Run with the Layer-2 stack up:

    docker compose -f docker-compose.integration.yml up -d --wait embedding-api
    EMBEDDING_TEST_URL=http://localhost:18003/embed uv run pytest \
        tests/integration/test_resolve_values_live_embedding.py -v
"""

from __future__ import annotations

import os

import pytest

from data_agent.runtime.auth.credentials import RuntimeCredentials
from data_agent.runtime.composite.resolve_values import ResolveValuesComposite
from data_agent.runtime.dispatch.tool_dispatcher import ToolDispatcher
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle

pytestmark = pytest.mark.skipif(
    not os.environ.get("EMBEDDING_TEST_URL"),
    reason="Requires a live custom embedding API (set EMBEDDING_TEST_URL).",
)

_T = "dbpcm_warehouse.accrual_events"
_CATALOG = CatalogHandle(
    {
        _T: {
            "EarnCode": "Nullable(String)",
            "EarnDescription": "Nullable(String)",
        }
    }
)


def _credentials() -> RuntimeCredentials:
    return RuntimeCredentials(
        session_id="sess-live-embed", jwt="jwt.body.sig", column_scope=frozenset()
    )


def _embedding_client() -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url=os.environ["EMBEDDING_TEST_URL"],
        api_key=os.environ.get("EMBEDDING_TEST_API_KEY", ""),
        model="all-mpnet-base-v2",
        timeout_seconds=30.0,
    )


async def test_semantic_ranking_beats_frequency_with_live_embedder() -> None:
    # OT is 20x more frequent than PTO — pure frequency would rank OT first.
    run_query_result = {
        "columns": ["EarnCode", "EarnDescription", "freq"],
        "rows": [
            ["PTO", "Paid Time Off", 5],
            ["OT", "Overtime", 100],
        ],
        "row_count": 2,
        "truncated": False,
    }
    mcp = FakeMCPClient(scripted={"runQuery": [run_query_result]})
    dispatcher = ToolDispatcher(mcp, _CATALOG)
    composite = ResolveValuesComposite(
        tool_dispatcher=dispatcher,
        catalog=_CATALOG,
        embedding_client=_embedding_client(),
        query_limit=200,
        top_k=10,
        similarity_weight=0.7,
    )

    tool_result = await composite.run(
        {"table": _T, "column": "EarnCode", "concept": "paid time off"},
        _credentials(),
    )

    assert tool_result.status == "ok"
    full = tool_result.result_full
    # The real embedder ran — NOT degraded to freq-only.
    assert full["degraded"] is False
    assert full["ranking"] == "semantic+freq"
    values = full["values"]
    # Semantic win: PTO first despite OT's 20x-higher frequency.
    assert [v["value"] for v in values] == ["PTO", "OT"]
    assert values[0]["score"] > values[1]["score"]
