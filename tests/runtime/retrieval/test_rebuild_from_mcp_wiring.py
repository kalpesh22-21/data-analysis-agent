"""Layer-1 tests for the Part C startup nuke + rebuild-from-MCP wiring (no infra).

The DESTRUCTIVE rebuild is armed at app startup (the lifespan) ONLY when
`neo4j_rebuild_from_mcp` is set AND this app owns a `Neo4jVectorIndex` (Neo4j present).
It fails LOUD when the flag is set without a JWT, and the full nuke→schema→catalog→
corpus sequence runs against the retrieval driver at the resolved dimension.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime import app as app_module
from data_agent.runtime.app import _rebuild_graph_from_mcp, create_app
from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.mcp.client import MCPToolSpec
from data_agent.runtime.mcp.fake_client import FakeMCPClient
from data_agent.runtime.model.client import ModelTurnResult
from data_agent.runtime.model.scripted_client import ScriptedModelClient
from data_agent.runtime.provenance.catalog_handle import CatalogHandle
from data_agent.runtime.retrieval.corpus_loader import (
    _CLAIM_REBUILD_LOCK,
    _EXISTING_VECTOR_DIMS,
    _NUKE_DELETE_NODES,
)
from data_agent.runtime.session.memory_store import InMemorySessionStore

_EMBED_URL = "http://embedding.local/embed"
_NEO4J_URL = "bolt://localhost:7687"


# ---------------------------------------------------------------------------
# Fakes for a full direct-call rebuild
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, *, row: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    async def single(self) -> dict[str, Any] | None:
        return self._row

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class _Runner:
    def __init__(self, driver: _Driver) -> None:
        self._driver = driver

    async def run(self, query: str, **params: Any) -> _Result:
        self._driver.calls.append((query, params))
        if query == _CLAIM_REBUILD_LOCK:
            return _Result(row={"claimed": True})
        if query == _EXISTING_VECTOR_DIMS:
            return _Result(rows=[])
        low = query.lower()
        if "models" in low or "embedding_model" in query:
            return _Result(row={"models": []})
        if "collect(column_key)" in query or "missing" in query:
            return _Result(row={"missing": []})
        if "deleted" in query:
            return _Result(row={"deleted": 0})
        return _Result(row=None, rows=[])


class _Session(_Runner):
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def execute_write(self, fn: Any) -> Any:
        return await fn(_Runner(self._driver))


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def session(self, *, database: str = "neo4j") -> _Session:  # noqa: ARG002
        return _Session(self)


class _Embedder:
    def __init__(self, dim: int = 8) -> None:
        # dim=8 matches the configured EMBEDDING_DIMENSION in `_settings` so the S1
        # config-vs-model cross-check in the rebuild's probe passes.
        self._dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]


class _CatalogClient:
    def __init__(self) -> None:
        self.creds: tuple[str, str] | None = None

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        self.creds = (jwt, session_id)
        return {"catalog_sha": "cat-sha", "catalog": {"db.t": {"columns": {"c": {"type": "String"}}}}}


class _CorpusClient:
    def __init__(self) -> None:
        self.creds: tuple[str, str] | None = None

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        self.creds = (jwt, session_id)
        return {
            "blueprints": {"bp-1": {"id": "bp-1", "intent": "i", "slots_summary": "", "uses": ["db.t.c"]}},
            "blueprints_sha": "bp-sha",
            "knowledge": {"kn-1": {"id": "kn-1", "text": "t", "doc_id": "d"}},
            "knowledge_sha": "kn-sha",
        }


def _settings(**over: Any) -> RuntimeSettings:
    base: dict[str, Any] = {
        "_env_file": None,
        "neo4j_rebuild_from_mcp": True,
        "rebuild_mcp_jwt": "jwt-abc",
        "rebuild_mcp_session_id": "sess-1",
        "embedding_model": "all-mpnet-base-v2",
        "embedding_dimension": 8,
    }
    base.update(over)
    return RuntimeSettings(**base)


# ---------------------------------------------------------------------------
# Direct _rebuild_graph_from_mcp behavior
# ---------------------------------------------------------------------------


async def test_rebuild_raises_when_flag_set_but_jwt_missing() -> None:
    with pytest.raises(RuntimeError) as exc:
        await _rebuild_graph_from_mcp(
            _settings(rebuild_mcp_jwt=""),
            driver=_Driver(),
            database="neo4j",
            embedding_client=_Embedder(),
        )
    assert "REBUILD_MCP_JWT" in str(exc.value)


async def test_rebuild_nukes_then_reseeds_at_the_configured_dimension() -> None:
    driver = _Driver()
    catalog_client = _CatalogClient()
    corpus_client = _CorpusClient()
    await _rebuild_graph_from_mcp(
        _settings(),
        driver=driver,
        database="neo4j",
        embedding_client=_Embedder(),
        catalog_client=catalog_client,
        corpus_client=corpus_client,
    )
    issued = [q for q, _ in driver.calls]
    # The lock was claimed, the graph nuked, and the schema recreated at dim=8 (config).
    assert _CLAIM_REBUILD_LOCK in issued
    assert _NUKE_DELETE_NODES in issued
    assert any("DROP INDEX" in q for q in issued)
    assert any("`vector.dimensions`: 8" in q for q in issued)
    # The MCP exports were fetched with the passed-in credentials.
    assert catalog_client.creds == ("jwt-abc", "sess-1")
    assert corpus_client.creds == ("jwt-abc", "sess-1")


async def test_rebuild_skips_when_lock_not_claimed() -> None:
    class _NoClaimDriver(_Driver):
        def session(self, *, database: str = "neo4j") -> _Session:  # noqa: ARG002
            return _NoClaimSession(self)

    class _NoClaimSession(_Session):
        async def run(self, query: str, **params: Any) -> _Result:
            self._driver.calls.append((query, params))
            if query == _CLAIM_REBUILD_LOCK:
                return _Result(row={"claimed": False})
            return _Result(row=None, rows=[])

    driver = _NoClaimDriver()
    await _rebuild_graph_from_mcp(
        _settings(),
        driver=driver,
        database="neo4j",
        embedding_client=_Embedder(),
        catalog_client=_CatalogClient(),
        corpus_client=_CorpusClient(),
    )
    issued = [q for q, _ in driver.calls]
    # Claimed and skipped — NOTHING destructive ran.
    assert _CLAIM_REBUILD_LOCK in issued
    assert _NUKE_DELETE_NODES not in issued


# ---------------------------------------------------------------------------
# Startup arming gate (through create_app's lifespan)
# ---------------------------------------------------------------------------


class _RecordingIndex:
    def __init__(self, **_: Any) -> None:
        self.driver = _Driver()
        self.database = "neo4j"

    async def recall(self, **_: Any) -> list[Any]:
        return []

    async def close(self) -> None:
        return None


def _make_app(monkeypatch, *, neo4j_url: str, flag: bool, jwt: str = "jwt-abc") -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {"called": 0}

    async def _fake_rebuild(_settings, **_kw):  # type: ignore[no-untyped-def]
        seen["called"] += 1

    monkeypatch.setattr(app_module, "Neo4jVectorIndex", _RecordingIndex)
    monkeypatch.setattr(app_module, "_rebuild_graph_from_mcp", _fake_rebuild)
    # Neutralize the per-turn corpus cache wiring so it never interferes.
    monkeypatch.setattr(app_module, "build_corpus_cache", lambda *_a, **_k: None)

    settings = RuntimeSettings(
        _env_file=None,
        neo4j_url=neo4j_url,
        neo4j_username="neo4j",
        neo4j_password="pw",
        embedding_api_url=_EMBED_URL,
        embedding_model="all-mpnet-base-v2",
        neo4j_rebuild_from_mcp=flag,
        rebuild_mcp_jwt=jwt,
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
    return app, seen


async def _run_lifespan(app: Any) -> None:
    async with app.router.lifespan_context(app):
        pass


async def test_startup_rebuild_armed_when_flag_and_neo4j_present(monkeypatch) -> None:
    app, seen = _make_app(monkeypatch, neo4j_url=_NEO4J_URL, flag=True)
    await _run_lifespan(app)
    assert seen["called"] == 1


async def test_startup_rebuild_not_armed_without_the_flag(monkeypatch) -> None:
    app, seen = _make_app(monkeypatch, neo4j_url=_NEO4J_URL, flag=False)
    await _run_lifespan(app)
    assert seen["called"] == 0


async def test_startup_rebuild_noop_when_neo4j_absent_even_with_flag(monkeypatch) -> None:
    # Neo4j absent → vector_index is None → the rebuild is a no-op (Phase-0 parity),
    # and NO raise despite the flag being set.
    app, seen = _make_app(monkeypatch, neo4j_url="", flag=True)
    await _run_lifespan(app)
    assert seen["called"] == 0
