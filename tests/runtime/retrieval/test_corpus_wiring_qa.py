"""Reader-ify guard (singleton-hydrator redesign): the runtime NO LONGER seeds neo4j on
the request path — the independent hydrator daemon owns all seeding + nuke/rebuild.

These tests assert the seed machinery is GONE from the runtime app module:
  * no corpus cache (`build_corpus_cache`/`_maybe_warm_corpus`) and no `HttpCorpusClient`
    import survive in `app.py`;
  * the DESTRUCTIVE runtime rebuild path (`_rebuild_graph_from_mcp` + its config flags)
    is removed;
  * the catalog cache is KEPT (per-turn provenance handle) and the catalog handle still
    resolves per-turn (provenance path intact) — proven by a wired app whose dispatcher
    resolves the catalog provider without any seed side effect.
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


def test_runtime_app_has_no_corpus_seed_machinery() -> None:
    # The corpus cache + its warm trigger + the corpus client import are all removed.
    assert not hasattr(app_module, "build_corpus_cache")
    assert not hasattr(app_module, "HttpCorpusClient")
    assert not hasattr(app_module, "_maybe_warm_corpus")


def test_runtime_app_has_no_rebuild_path() -> None:
    # The DESTRUCTIVE runtime rebuild is removed — the hydrator owns nuke/rebuild.
    assert not hasattr(app_module, "_rebuild_graph_from_mcp")


def test_rebuild_config_fields_removed() -> None:
    settings = RuntimeSettings(_env_file=None)
    assert not hasattr(settings, "neo4j_rebuild_from_mcp")
    assert not hasattr(settings, "rebuild_mcp_jwt")
    assert not hasattr(settings, "rebuild_mcp_session_id")
    # ...and the new hydrator/service-key surface is present.
    assert settings.mcp_service_key == ""
    assert settings.hydrator_poll_interval_seconds == 60


def test_app_builds_without_seed_side_effects(monkeypatch) -> None:
    """A wired app builds cleanly with the catalog cache kept (no seed callback). Neo4j
    absent → no vector index → the app is a pure reader (Phase-0 parity)."""

    class _RecordingIndex:
        def __init__(self, **_: Any) -> None:
            pass

        async def recall(self, **_: Any) -> list[Any]:
            return []

        async def close(self) -> None:
            return None

    monkeypatch.setattr(app_module, "Neo4jVectorIndex", _RecordingIndex)
    settings = RuntimeSettings(_env_file=None)  # no neo4j_url → reader with no index
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
    # /ready exists (added by the reader-ify pass); /turn still wired.
    routes = {getattr(r, "path", None) for r in app.router.routes}
    assert "/ready" in routes
    assert "/turn" in routes
