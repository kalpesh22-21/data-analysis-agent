"""The unauthenticated `GET /ready` graph-readiness gate (singleton-hydrator redesign).

`/ready` returns 200 once the hydrator has seeded neo4j (the `:CorpusMeta.corpus_sha`
singleton is present) and 503 otherwise, WITHOUT any auth headers. When the app owns no
`Neo4jVectorIndex` (Neo4j absent / Phase-0 parity) it reports ready immediately (nothing
to seed).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_agent.runtime import app as app_module
from data_agent.runtime.app import create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.session.memory_store import InMemorySessionStore

_EMBED_URL = "http://embedding.local/embed"
_NEO4J_URL = "bolt://localhost:7687"


class _ReadyIndex:
    """Neo4jVectorIndex stand-in with a scripted `graph_ready`."""

    def __init__(self, *, ready: bool = True, **_: Any) -> None:
        self._ready = ready

    async def recall(self, **_: Any) -> list[Any]:
        return []

    async def graph_ready(self) -> bool:
        return self._ready

    async def close(self) -> None:
        return None


def _app(monkeypatch, *, neo4j_url: str, ready: bool) -> TestClient:
    monkeypatch.setattr(
        app_module, "Neo4jVectorIndex", lambda **kw: _ReadyIndex(ready=ready, **kw)
    )
    settings = RuntimeSettings(
        _env_file=None,
        neo4j_url=neo4j_url,
        neo4j_username="neo4j",
        neo4j_password="pw",
        embedding_api_url=_EMBED_URL,
        embedding_model="all-mpnet-base-v2",
    )
    app = create_app(
        settings=settings,
        session_store=InMemorySessionStore(),
        mcp_client=FakeMCPClient(
            tools=[
                MCPToolSpec(
                    name="listDatabases",
                    description="",
                    input_schema={"type": "object", "properties": {}},
                )
            ],
            scripted={},
        ),
        model_client=ScriptedModelClient([ModelTurnResult(assistant_text="done")]),
        catalog=CatalogHandle({}),
    )
    return TestClient(app)


def test_ready_200_when_corpus_sha_present(monkeypatch) -> None:
    with _app(monkeypatch, neo4j_url=_NEO4J_URL, ready=True) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"ready": True}


def test_ready_503_when_corpus_sha_absent(monkeypatch) -> None:
    with _app(monkeypatch, neo4j_url=_NEO4J_URL, ready=False) as client:
        resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {"ready": False}


def test_ready_200_when_no_vector_index(monkeypatch) -> None:
    # Neo4j absent → vector_index is None → nothing to seed → always ready.
    with _app(monkeypatch, neo4j_url="", ready=False) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"ready": True}


def test_ready_requires_no_auth_headers(monkeypatch) -> None:
    # No Authorization / X-Session-Id headers — the probe must not 401/400.
    with _app(monkeypatch, neo4j_url=_NEO4J_URL, ready=True) as client:
        resp = client.get("/ready")
    assert resp.status_code == 200


@pytest.mark.parametrize("ready", [True, False])
def test_ready_never_calls_extract_credentials(monkeypatch, ready: bool) -> None:
    # Guard: if /ready ever routed through _extract_credentials, a missing JWT would 401.
    with _app(monkeypatch, neo4j_url=_NEO4J_URL, ready=ready) as client:
        resp = client.get("/ready")
    assert resp.status_code in (200, 503)
