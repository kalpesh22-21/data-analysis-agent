"""QA2 extension of the Slice-2 app-wiring gate (neo4j-corpus-design §2.4).

`test_neo4j_wiring_qa.py` covers the 4-combo (neo4j_url × embedder) truth table
and injected-precedence. This file EXTENDS it (no duplication) with:

  * reranker-optional: url+embedder with/without `reranker_api_url` -> the
    pipeline is built either way, reranker present only when configured.
  * settings with a store+embedder but an EMPTY password -> still wired (the
    password is not part of the gate; empty auth is passed through).
  * `retrieval_enabled=False` -> the assembler gets no retrieval, but the
    Neo4jVectorIndex + driver pool are STILL constructed (FLAG).
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
    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        _RecordingIndex.instances.append(kwargs)

    async def recall(self, **_: Any) -> list[Any]:
        return []

    async def close(self) -> None:
        return None


class _RecordingPipeline:
    """Captures the kwargs `create_app` passes to `RetrievalPipeline` so we can
    assert the reranker leg without a real pipeline."""

    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        _RecordingPipeline.instances.append(kwargs)


class _RecordingReranker:
    instances: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        _RecordingReranker.instances.append(kwargs)


def _patch(monkeypatch) -> None:
    _RecordingIndex.instances.clear()
    _RecordingPipeline.instances.clear()
    _RecordingReranker.instances.clear()
    monkeypatch.setattr(app_module, "Neo4jVectorIndex", _RecordingIndex)
    monkeypatch.setattr(app_module, "RetrievalPipeline", _RecordingPipeline)
    monkeypatch.setattr(app_module, "HttpRerankerClient", _RecordingReranker)


def _mcp() -> FakeMCPClient:
    return FakeMCPClient(
        tools=[
            MCPToolSpec(
                name="listDatabases",
                description="",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        scripted={},
    )


def _create(settings: RuntimeSettings) -> None:
    create_app(
        settings=settings,
        session_store=InMemorySessionStore(),
        mcp_client=_mcp(),
        model_client=ScriptedModelClient([ModelTurnResult(assistant_text="done")]),
        catalog=CatalogHandle({}),
    )


def _settings(**over: Any) -> RuntimeSettings:
    base: dict[str, Any] = {
        "_env_file": None,
        "neo4j_url": "bolt://localhost:7687",
        "neo4j_username": "neo4j",
        "neo4j_password": "pw",
        "embedding_api_url": _EMBED_URL,
        "embedding_model": "all-mpnet-base-v2",
    }
    base.update(over)
    return RuntimeSettings(**base)


# --------------------------------------------------------------------------
# Reranker-optional combos
# --------------------------------------------------------------------------


def test_pipeline_built_without_reranker_when_url_absent(monkeypatch) -> None:
    _patch(monkeypatch)
    _create(_settings(reranker_api_url=""))
    assert len(_RecordingPipeline.instances) == 1
    # reranker degrades to None -> recall-order (design §2.4).
    assert _RecordingPipeline.instances[0]["reranker"] is None
    assert _RecordingReranker.instances == []


def test_pipeline_built_with_reranker_when_url_present(monkeypatch) -> None:
    _patch(monkeypatch)
    _create(_settings(reranker_api_url="http://reranker.local/rerank"))
    assert len(_RecordingPipeline.instances) == 1
    assert _RecordingPipeline.instances[0]["reranker"] is not None
    assert len(_RecordingReranker.instances) == 1
    assert _RecordingReranker.instances[0]["url"] == "http://reranker.local/rerank"


def test_pipeline_receives_the_constructed_vector_index(monkeypatch) -> None:
    _patch(monkeypatch)
    _create(_settings(reranker_api_url=""))
    assert len(_RecordingIndex.instances) == 1
    vi = _RecordingPipeline.instances[0]["vector_index"]
    assert isinstance(vi, _RecordingIndex)


# --------------------------------------------------------------------------
# Empty password — not part of the gate
# --------------------------------------------------------------------------


def test_empty_password_still_wires_index_with_empty_auth(monkeypatch) -> None:
    # The gate is (neo4j_url AND embedder). Auth is passed through verbatim even
    # when the password is "" — a misconfigured secret does NOT disable wiring;
    # it will surface later as a driver auth failure that recall degrades to [].
    _patch(monkeypatch)
    _create(_settings(neo4j_password=""))
    assert len(_RecordingIndex.instances) == 1
    assert _RecordingIndex.instances[0]["auth"] == ("neo4j", "")


def test_empty_username_and_password_still_wires(monkeypatch) -> None:
    _patch(monkeypatch)
    _create(_settings(neo4j_username="", neo4j_password=""))
    assert len(_RecordingIndex.instances) == 1
    assert _RecordingIndex.instances[0]["auth"] == ("", "")


# --------------------------------------------------------------------------
# retrieval_enabled master switch
# --------------------------------------------------------------------------


def test_retrieval_disabled_constructs_no_index(monkeypatch) -> None:
    # S4 FIX (was: pinned that a disabled deployment STILL opened a driver pool).
    # `retrieval_enabled=False` now also gates the index-construction block, so a
    # force-disabled deployment opens NO driver pool and builds no pipeline.
    _patch(monkeypatch)
    _create(_settings(retrieval_enabled=False))
    assert _RecordingIndex.instances == []  # no idle driver pool
    assert _RecordingPipeline.instances == []  # no pipeline built


def test_retrieval_disabled_without_store_still_unwired(monkeypatch) -> None:
    # Control: with the switch off AND no store, nothing is constructed.
    _patch(monkeypatch)
    _create(_settings(neo4j_url="", retrieval_enabled=False))
    assert _RecordingIndex.instances == []
