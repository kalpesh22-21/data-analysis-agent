"""QA tests for the Slice-1 retrieval settings + the app master switch.

Covers `RuntimeSettings` retrieval defaults/validation (design §3.4) and the
one place `retrieval_enabled` is actually honored — `app.py` passes
`retrieval=None` when the switch is off, so a wired pipeline is fully bypassed
(Phase-0 parity), even when a pipeline object was handed to `create_app`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.model.embedding_client import FakeEmbeddingClient
from data_agent.runtime.model.reranker_client import FakeRerankerClient
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.models import Candidate
from data_agent.runtime.retrieval.pipeline import RetrievalPipeline
from data_agent.runtime.retrieval.user_memory import NullUserMemoryProvider
from data_agent.runtime.retrieval.vector_index import FakeVectorIndex
from data_agent.runtime.session.memory_store import InMemorySessionStore

SESSION_ID = "sess-retr-cfg"
HEADERS = {"Authorization": "Bearer test-jwt", "X-Session-Id": SESSION_ID}


# --------------------------------------------------------------------------
# RuntimeSettings — retrieval defaults + validation (design §3.4)
# --------------------------------------------------------------------------


def test_retrieval_defaults() -> None:
    s = RuntimeSettings(_env_file=None)
    assert s.retrieval_enabled is True  # master switch on by default
    assert s.retrieval_prefetch_tool_enabled is False
    assert s.retrieval_recall_k == 30
    assert s.retrieval_top_k_blueprints == 3  # 03 fixes 3
    assert s.retrieval_top_k_knowledge == 3
    assert s.retrieval_knowledge_min_score is None  # floor off (OQ-R2)
    assert s.neo4j_url == ""  # unconfigured => Slice-2 index unavailable
    assert s.reranker_model == ""


def test_top_k_and_recall_k_must_be_at_least_one() -> None:
    for field, value in [
        ("retrieval_recall_k", 0),
        ("retrieval_recall_k", -5),
        ("retrieval_top_k_blueprints", 0),
        ("retrieval_top_k_knowledge", -1),
    ]:
        with pytest.raises(ValidationError):
            RuntimeSettings(_env_file=None, **{field: value})


def test_recall_k_below_top_k_is_permitted_no_cross_field_validation() -> None:
    # DOCUMENTED GAP (design §3.4 / OQ-R1): there is no invariant that
    # recall_k >= top_k. A misconfiguration where recall_k < top_k is accepted
    # and silently caps candidates below the requested top-N (see the pipeline
    # characterization test). Slice 2 should decide whether to enforce it.
    s = RuntimeSettings(_env_file=None, retrieval_recall_k=2, retrieval_top_k_blueprints=10)
    assert s.retrieval_recall_k == 2
    assert s.retrieval_top_k_blueprints == 10


def test_knowledge_min_score_can_be_set() -> None:
    s = RuntimeSettings(_env_file=None, retrieval_knowledge_min_score=0.25)
    assert s.retrieval_knowledge_min_score == 0.25


# --------------------------------------------------------------------------
# app.py master switch: retrieval_enabled=False disables a WIRED pipeline
# --------------------------------------------------------------------------


def _recording_pipeline(embedder: FakeEmbeddingClient) -> RetrievalPipeline:
    index = FakeVectorIndex(
        [
            (
                Candidate(
                    id="bp-1",
                    kind="blueprint",
                    text="sales overtime rollup",
                    uses=frozenset(),
                    payload={"intent": "sales overtime rollup", "slots_summary": "dept"},
                ),
                [1.0, 0.0],
            )
        ]
    )
    return RetrievalPipeline(
        embedding_client=embedder,
        reranker=FakeRerankerClient(),
        vector_index=index,
        user_memory=NullUserMemoryProvider(),
        recall_k=30,
        top_k_blueprints=3,
        top_k_knowledge=3,
    )


def _client(monkeypatch, *, retrieval_enabled: bool, embedder: FakeEmbeddingClient) -> TestClient:
    monkeypatch.setattr(app_module, "verify_jwt", lambda *a, **k: frozenset())
    mcp_client = FakeMCPClient(
        tools=[MCPToolSpec(name="listDatabases", description="", input_schema={"type": "object", "properties": {}})],
        scripted={},
    )
    settings = RuntimeSettings(
        _env_file=None,
        max_loop_iterations=15,
        max_wall_clock_seconds=60,
        max_budget_windows=3,
        retrieval_enabled=retrieval_enabled,
    )
    app = create_app(
        settings=settings,
        session_store=InMemorySessionStore(),
        mcp_client=mcp_client,
        model_client=ScriptedModelClient([ModelTurnResult(assistant_text="done")]),
        catalog=CatalogHandle({}),
        retrieval=_recording_pipeline(embedder),
    )
    return TestClient(app)


def test_master_switch_off_bypasses_a_wired_pipeline(monkeypatch) -> None:
    embedder = FakeEmbeddingClient()
    client = _client(monkeypatch, retrieval_enabled=False, embedder=embedder)
    resp = client.post("/turn", json={"message": "sales overtime"}, headers=HEADERS)
    assert resp.status_code == 200
    # retrieval_enabled=False => app.py passes retrieval=None => never embedded.
    assert embedder.calls == []


def test_master_switch_on_runs_the_wired_pipeline(monkeypatch) -> None:
    embedder = FakeEmbeddingClient()
    client = _client(monkeypatch, retrieval_enabled=True, embedder=embedder)
    resp = client.post("/turn", json={"message": "sales overtime"}, headers=HEADERS)
    assert resp.status_code == 200
    # Switch on => the turn's question is embedded exactly once (memoized).
    assert embedder.calls == [["sales overtime"]]
