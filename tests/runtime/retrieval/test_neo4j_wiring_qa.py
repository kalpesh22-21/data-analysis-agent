"""Layer-1 tests for the Slice-2 app wiring gate (neo4j-corpus-design §2.4).

`app.py` constructs a `Neo4jVectorIndex`-backed `RetrievalPipeline` ONLY when
BOTH `neo4j_url` and an embedder are configured; absent either, retrieval stays
`None` (Phase-0 parity). `Neo4jVectorIndex` is monkeypatched with a recorder so
the gate is asserted without a real driver or a live neo4j.
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
    """Stand-in for `Neo4jVectorIndex` that records constructor kwargs and never
    touches a driver."""

    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        _RecordingIndex.instances.append(kwargs)

    async def recall(self, **_: Any) -> list[Any]:
        return []

    async def close(self) -> None:
        return None


def _build(monkeypatch, *, neo4j_url: str, embedding_url: str, **settings_over: Any) -> None:
    _RecordingIndex.instances.clear()
    monkeypatch.setattr(app_module, "Neo4jVectorIndex", _RecordingIndex)
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "neo4j_url": neo4j_url,
        "neo4j_username": "neo4j",
        "neo4j_password": "pw",
        "embedding_api_url": embedding_url,
        "embedding_model": "all-mpnet-base-v2",
    }
    kwargs.update(settings_over)
    settings = RuntimeSettings(**kwargs)
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


def test_unconfigured_neo4j_leaves_retrieval_unwired(monkeypatch) -> None:
    _build(monkeypatch, neo4j_url="", embedding_url=_EMBED_URL)
    assert _RecordingIndex.instances == []  # no store => never constructed


def test_neo4j_without_embedder_leaves_retrieval_unwired(monkeypatch) -> None:
    _build(monkeypatch, neo4j_url="bolt://localhost:7687", embedding_url="")
    assert _RecordingIndex.instances == []  # store but no embedder => still None


def test_neo4j_and_embedder_wires_the_index_with_expected_args(monkeypatch) -> None:
    _build(monkeypatch, neo4j_url="bolt://localhost:7687", embedding_url=_EMBED_URL)
    assert len(_RecordingIndex.instances) == 1
    kwargs = _RecordingIndex.instances[0]
    assert kwargs["url"] == "bolt://localhost:7687"
    assert kwargs["auth"] == ("neo4j", "pw")
    assert kwargs["expected_model"] == "all-mpnet-base-v2"


def test_injected_retrieval_is_not_rebuilt(monkeypatch) -> None:
    # An injected pipeline (tests) owns its own store; app.py must not construct
    # a Neo4jVectorIndex even when neo4j_url is set.
    _RecordingIndex.instances.clear()
    monkeypatch.setattr(app_module, "Neo4jVectorIndex", _RecordingIndex)
    settings = RuntimeSettings(
        _env_file=None,
        neo4j_url="bolt://localhost:7687",
        embedding_api_url=_EMBED_URL,
        embedding_model="all-mpnet-base-v2",
    )

    class _Sentinel:
        pass

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
        retrieval=_Sentinel(),  # type: ignore[arg-type]
    )
    assert _RecordingIndex.instances == []


def test_retrieval_disabled_constructs_no_index_or_driver(monkeypatch) -> None:
    # S4: retrieval_enabled=False gates the index-construction block too, so a
    # store+embedder deployment with retrieval disabled opens NO driver pool.
    _build(
        monkeypatch,
        neo4j_url="bolt://localhost:7687",
        embedding_url=_EMBED_URL,
        retrieval_enabled=False,
    )
    assert _RecordingIndex.instances == []


def test_empty_embedding_model_warns_but_still_wires(monkeypatch, caplog) -> None:
    # B2: embedding_model is the read-path parity key. An empty value would
    # parity-filter recall on '' and return an empty corpus — the wiring emits a
    # loud server-side warning (it still wires; the operator sees the warning).
    import logging

    with caplog.at_level(logging.WARNING, logger="data_agent.runtime.app"):
        _build(
            monkeypatch,
            neo4j_url="bolt://localhost:7687",
            embedding_url=_EMBED_URL,
            embedding_model="",
        )
    assert len(_RecordingIndex.instances) == 1
    assert any(
        "embedding_model is empty" in rec.getMessage() for rec in caplog.records
    ), caplog.records
