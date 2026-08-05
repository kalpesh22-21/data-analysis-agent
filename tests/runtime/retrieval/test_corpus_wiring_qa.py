"""Layer-1 tests for the governed-corpus app wiring gate (Phase 2, §E).

The corpus seed cache + `on_corpus_loaded` callback are wired ONLY when THIS app owns a
`Neo4jVectorIndex` (retrieval/neo4j present). When Neo4j is absent the feature is a
byte-identical Phase-0 no-op: no cache is built, no callback armed, no fetch triggered.
`build_corpus_cache` is monkeypatched with a recorder so the gate is asserted without a
real driver or MCP.
"""

from __future__ import annotations

from typing import Any

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


class _RecordingIndex:
    """Stand-in for `Neo4jVectorIndex` — records nothing, exposes no driver (proving the
    corpus wiring never touches `.driver` at BUILD time; the seed reads it lazily)."""

    def __init__(self, **_: Any) -> None:
        pass

    async def recall(self, **_: Any) -> list[Any]:
        return []

    async def close(self) -> None:
        return None


def _build(monkeypatch, *, neo4j_url: str, embedding_url: str) -> dict[str, Any]:
    calls: dict[str, Any] = {"count": 0, "on_corpus_loaded": "unset"}

    def _fake_build_corpus_cache(_settings, *, on_corpus_loaded=None):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        calls["on_corpus_loaded"] = on_corpus_loaded
        return None

    monkeypatch.setattr(app_module, "Neo4jVectorIndex", _RecordingIndex)
    monkeypatch.setattr(app_module, "build_corpus_cache", _fake_build_corpus_cache)

    settings = RuntimeSettings(
        _env_file=None,
        neo4j_url=neo4j_url,
        neo4j_username="neo4j",
        neo4j_password="pw",
        embedding_api_url=embedding_url,
        embedding_model="all-mpnet-base-v2",
    )
    create_app(
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
    return calls


def test_no_neo4j_builds_no_corpus_cache(monkeypatch) -> None:
    # Neo4j absent → vector_index is None → the corpus feature is entirely absent.
    calls = _build(monkeypatch, neo4j_url="", embedding_url=_EMBED_URL)
    assert calls["count"] == 0


def test_neo4j_without_embedder_builds_no_corpus_cache(monkeypatch) -> None:
    # Store but no embedder → vector_index is None → no corpus cache.
    calls = _build(monkeypatch, neo4j_url="bolt://localhost:7687", embedding_url="")
    assert calls["count"] == 0


def test_neo4j_and_embedder_wires_corpus_cache_with_a_callback(monkeypatch) -> None:
    calls = _build(monkeypatch, neo4j_url="bolt://localhost:7687", embedding_url=_EMBED_URL)
    assert calls["count"] == 1
    # A real seed callback is armed (not None) — it fires load_corpus on the first turn.
    assert callable(calls["on_corpus_loaded"])
