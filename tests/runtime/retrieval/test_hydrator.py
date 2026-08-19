"""Layer-1 tests for the singleton hydrator daemon (no infra).

The hydrator fetches the MCP catalog + corpus exports via the service-key clients and
seeds neo4j idempotently (`apply_schema` → `load_catalog_graph` → `load_corpus`, gc=True)
on a poll loop. Covered:
  * `run_once` seeds via the loaders (recorded on a fake driver);
  * an unchanged poll is a B1 no-op (the loaders' sha guards skip the re-embed);
  * a dimension change → SCOPED `rebuild_mcp_corpus_partition` (never the full `nuke_graph`)
    + reseed; a same-dim model swap clears the mcp partition + re-embeds; both preserve the
    learning tier; a residual `DimensionMismatchError` is the caught safety net;
  * an atomic scoped-delete survives a mid-rebuild crash (next cycle reseeds, no sha-skip);
  * the kill-switch disables a cycle (no fetch, no write);
  * `run_forever` is driven by an injected `sleep` that stops after N cycles;
  * `build_hydrator` returns `None` (idles, no crash) when neo4j / embedding / the service
    key are absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.retrieval import hydrator as hydrator_mod
from data_agent.runtime.retrieval.corpus_loader import (
    _DELETE_FRESHNESS_SINGLETONS,
    _DELETE_MCP_CORPUS_NODES,
    _EXISTING_VECTOR_DIMS,
    _NUKE_DELETE_NODES,
    _READ_CATALOG_META,
    _READ_CORPUS_META,
    rebuild_mcp_corpus_partition,
)
from data_agent.runtime.retrieval.hydrator import Hydrator, build_hydrator

_CAT_SHA = "cat-sha"
_CORPUS_SHA = "bp-sha:kn-sha"  # effective_corpus_sha of the fake corpus export below


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(
        self, *, row: dict[str, Any] | None = None, rows: list[dict[str, Any]] | None = None
    ) -> None:
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
        self._driver.calls.append(query)
        if "DROP INDEX" in query:
            self._driver.dropped_indexes = True
        if _DELETE_MCP_CORPUS_NODES.strip() in query:
            self._driver.mcp_deleted = True
        if _DELETE_FRESHNESS_SINGLETONS.strip() in query:
            self._driver.metas_deleted = True
        if query == _EXISTING_VECTOR_DIMS:
            # After the indexes are dropped they are gone (fresh); before, report the
            # (possibly mismatched) existing dims the driver was seeded with.
            rows = [] if self._driver.dropped_indexes else self._driver.existing_dim_rows
            return _Result(rows=rows)
        if query == _READ_CATALOG_META:
            sha = None if self._driver.metas_deleted else self._driver.catalog_meta
            return _Result(row={"catalog_sha": sha})
        if query == _READ_CORPUS_META:
            sha = None if self._driver.metas_deleted else self._driver.corpus_meta
            return _Result(row={"corpus_sha": sha})
        low = query.lower()
        if "models" in low or "embedding_model" in query:
            # The mcp partition's models — empty once the scoped delete cleared them.
            models = [] if self._driver.mcp_deleted else self._driver.existing_models
            return _Result(row={"models": models})
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
    def __init__(
        self,
        *,
        existing_dim_rows: list[dict[str, Any]] | None = None,
        existing_models: list[str] | None = None,
        catalog_meta: str | None = None,
        corpus_meta: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.existing_dim_rows = existing_dim_rows or []
        self.existing_models = existing_models or []
        self.catalog_meta = catalog_meta
        self.corpus_meta = corpus_meta
        self.dropped_indexes = False
        self.mcp_deleted = False
        self.metas_deleted = False
        self.closed = False

    def session(self, *, database: str = "neo4j") -> _Session:  # noqa: ARG002
        return _Session(self)

    async def close(self) -> None:
        self.closed = True


class _Embedder:
    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.embed_calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.0] * self._dim for _ in texts]


class _CatalogClient:
    def __init__(self) -> None:
        self.fetches = 0

    async def fetch_export(self, *, jwt: str = "", session_id: str = "") -> dict[str, Any]:
        self.fetches += 1
        return {"catalog_sha": _CAT_SHA, "catalog": {"db.t": {"columns": {"c": {"type": "String"}}}}}


class _CorpusClient:
    def __init__(self) -> None:
        self.fetches = 0

    async def fetch_export(self, *, jwt: str = "", session_id: str = "") -> dict[str, Any]:
        self.fetches += 1
        return {
            "blueprints": {
                "bp-1": {"id": "bp-1", "intent": "i", "slots_summary": "", "uses": ["db.t.c"]}
            },
            "blueprints_sha": "bp-sha",
            "knowledge": {"kn-1": {"id": "kn-1", "text": "t", "doc_id": "d"}},
            "knowledge_sha": "kn-sha",
        }


def _hydrator(driver: _Driver, embedder: _Embedder | None = None, **over: Any) -> Hydrator:
    return Hydrator(
        driver=driver,
        database="neo4j",
        embedding_client=embedder or _Embedder(),
        catalog_client=over.pop("catalog_client", _CatalogClient()),
        corpus_client=over.pop("corpus_client", _CorpusClient()),
        model_id="all-mpnet-base-v2",
        configured_dimension=8,  # avoids a probe embed; matches the fake embedder dim
        poll_interval_seconds=60,
    )


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


async def test_run_once_seeds_via_loaders(monkeypatch) -> None:
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: True)
    driver = _Driver()
    embedder = _Embedder()
    catalog_client = _CatalogClient()
    corpus_client = _CorpusClient()
    h = _hydrator(
        driver, embedder, catalog_client=catalog_client, corpus_client=corpus_client
    )

    assert await h.run_once() is True

    # Both exports fetched; the corpus embedded; the schema + upserts issued; NO delete.
    assert catalog_client.fetches == 1
    assert corpus_client.fetches == 1
    assert embedder.embed_calls >= 1
    assert any("CREATE VECTOR INDEX" in q for q in driver.calls)
    assert any("MERGE (t:Table" in q for q in driver.calls)
    assert not driver.dropped_indexes
    assert not driver.mcp_deleted
    assert _NUKE_DELETE_NODES not in driver.calls


async def test_unchanged_poll_is_a_b1_no_op(monkeypatch) -> None:
    # Both freshness singletons already carry this run's shas → the loaders skip the
    # re-embed + write (B1 no-op). apply_schema still runs (idempotent), but NO embed.
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: True)
    driver = _Driver(catalog_meta=_CAT_SHA, corpus_meta=_CORPUS_SHA)
    embedder = _Embedder()
    h = _hydrator(driver, embedder)

    assert await h.run_once() is True
    # The corpus was NOT re-embedded (the expensive part was skipped).
    assert embedder.embed_calls == 0
    # No blueprint upsert ran (the corpus load short-circuited before writing).
    assert not any("MERGE (b:Blueprint" in q for q in driver.calls)


async def test_dimension_change_does_a_scoped_rebuild_not_a_full_nuke(monkeypatch) -> None:
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: True)
    # A pre-existing index at dim 384 while the hydrator targets 8 → dimension change →
    # SCOPED rebuild_mcp_corpus_partition (drop+recreate indexes at 8, delete ONLY the
    # source='mcp' corpus + metas) → reseed. NEVER the full nuke (learning tier preserved).
    driver = _Driver(
        existing_dim_rows=[{"name": "blueprint_intent_vec", "dimensions": 384}],
        catalog_meta=_CAT_SHA,
        corpus_meta=_CORPUS_SHA,
    )
    embedder = _Embedder()
    h = _hydrator(driver, embedder)

    assert await h.run_once() is True
    # The scoped delete ran (source='mcp'-scoped), the indexes were dropped + recreated
    # at the new dimension, and the corpus was re-embedded — but the FULL nuke did NOT run.
    assert driver.mcp_deleted is True
    assert driver.dropped_indexes is True
    assert _NUKE_DELETE_NODES not in driver.calls
    assert any(_DELETE_MCP_CORPUS_NODES.strip() in q for q in driver.calls)
    assert embedder.embed_calls >= 1
    assert any("`vector.dimensions`: 8" in q for q in driver.calls)


async def test_same_dim_model_swap_reembeds_and_preserves_learning(monkeypatch) -> None:
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: True)
    # The mcp partition carries an OLD embedding model at the SAME dimension (8) the
    # hydrator targets → model change → clear the source='mcp' partition (NO index drop)
    # then re-embed at the new model. Metas set to prove we BYPASS the sha B1 skip.
    driver = _Driver(
        existing_dim_rows=[{"name": "blueprint_intent_vec", "dimensions": 8}],
        existing_models=["old-model"],
        catalog_meta=_CAT_SHA,
        corpus_meta=_CORPUS_SHA,
    )
    embedder = _Embedder()
    h = _hydrator(driver, embedder)

    assert await h.run_once() is True
    # Scoped mcp delete ran; the corpus was re-embedded at the new model...
    assert driver.mcp_deleted is True
    assert embedder.embed_calls >= 1
    assert any("MERGE (b:Blueprint" in q for q in driver.calls)
    # ...WITHOUT dropping the vector index (same dimension) and WITHOUT the full nuke
    # (so the source='learning' staging tier survives).
    assert driver.dropped_indexes is False
    assert not any("DROP INDEX" in q for q in driver.calls)
    assert _NUKE_DELETE_NODES not in driver.calls


async def test_dimension_mismatch_error_safety_net_routes_to_scoped_rebuild(monkeypatch) -> None:
    # If a DimensionMismatchError still surfaces from the seed (the pre-check missed it),
    # run_once catches it and routes to the SCOPED rebuild — never a forever-loop.
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: True)
    driver = _Driver()
    embedder = _Embedder()
    h = _hydrator(driver, embedder)

    calls = {"seed": 0}
    real_seed = h._seed
    from data_agent.runtime.retrieval.corpus_loader import DimensionMismatchError

    async def _seed_once_raises(catalog_export, corpus_export, dimension):  # type: ignore[no-untyped-def]
        calls["seed"] += 1
        if calls["seed"] == 1:
            raise DimensionMismatchError("stale index the pre-check missed")
        return await real_seed(catalog_export, corpus_export, dimension)

    monkeypatch.setattr(h, "_seed", _seed_once_raises)

    assert await h.run_once() is True
    # The scoped rebuild ran (not the full nuke) and the seed was retried to success.
    assert driver.mcp_deleted is True
    assert _NUKE_DELETE_NODES not in driver.calls
    assert calls["seed"] == 2


async def test_crash_after_scoped_delete_next_cycle_reseeds_not_sha_skip(monkeypatch) -> None:
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: True)
    # Simulate a mid-rebuild crash: the ATOMIC scoped-delete txn committed (mcp nodes AND
    # BOTH freshness singletons gone) but the DDL/reseed never ran (a blip run_forever
    # swallowed, or a pod kill). Because the two deletes commit together, :CorpusMeta is
    # gone too — so the NEXT cycle must NOT B1 sha-skip into permanently-empty recall; it
    # must re-embed + reseed. (Were the deletes non-atomic, a crash between them could
    # leave :CorpusMeta present at the old sha and the reseed would sha-skip forever.)
    driver = _Driver(catalog_meta=_CAT_SHA, corpus_meta=_CORPUS_SHA)
    await rebuild_mcp_corpus_partition(driver, dimension=None, database="neo4j")
    assert driver.mcp_deleted is True
    assert driver.metas_deleted is True  # the singleton delete committed atomically

    embedder = _Embedder()
    h = _hydrator(driver, embedder)
    # The next poll cycle after the crash re-embeds + reseeds (no sha-skip into empty recall).
    assert await h.run_once() is True
    assert embedder.embed_calls >= 1
    assert any("MERGE (b:Blueprint" in q for q in driver.calls)


async def test_kill_switch_disables_the_cycle(monkeypatch) -> None:
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: False)
    driver = _Driver()
    catalog_client = _CatalogClient()
    h = _hydrator(driver, catalog_client=catalog_client)

    assert await h.run_once() is False
    # Disabled ⇒ no fetch, no write.
    assert catalog_client.fetches == 0
    assert driver.calls == []


# ---------------------------------------------------------------------------
# run_forever
# ---------------------------------------------------------------------------


async def test_run_forever_polls_until_sleep_stops(monkeypatch) -> None:
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: True)
    driver = _Driver()
    h = _hydrator(driver)

    cycles = {"n": 0}

    class _StopError(Exception):
        pass

    async def _sleep(_seconds: float) -> None:
        cycles["n"] += 1
        if cycles["n"] >= 3:
            raise _StopError

    with pytest.raises(_StopError):
        await h.run_forever(sleep=_sleep)
    # run_once ran once per interval before each sleep: 3 sleeps ⇒ 3 cycles.
    assert cycles["n"] == 3


async def test_run_forever_swallows_a_cycle_error_and_retries(monkeypatch) -> None:
    monkeypatch.setattr(hydrator_mod, "hydrator_enabled", lambda: True)
    driver = _Driver()
    h = _hydrator(driver)

    calls = {"n": 0}

    async def _boom() -> bool:
        calls["n"] += 1
        raise RuntimeError("transient neo4j blip")

    monkeypatch.setattr(h, "run_once", _boom)

    class _StopError(Exception):
        pass

    async def _sleep(_seconds: float) -> None:
        if calls["n"] >= 2:
            raise _StopError

    # The daemon must not die on a raised cycle — it logs and retries next interval.
    with pytest.raises(_StopError):
        await h.run_forever(sleep=_sleep)
    assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# build_hydrator gate (Neo4j/embedding absent → None, idles, no crash)
# ---------------------------------------------------------------------------


def test_build_hydrator_none_without_neo4j() -> None:
    settings = RuntimeSettings(
        _env_file=None, neo4j_url="", embedding_api_url="http://e/embed"
    )
    assert build_hydrator(settings) is None


def test_build_hydrator_none_without_embedding() -> None:
    settings = RuntimeSettings(
        _env_file=None, neo4j_url="bolt://localhost:7687", embedding_api_url=""
    )
    assert build_hydrator(settings) is None


def test_build_hydrator_none_and_logs_when_service_key_empty(caplog) -> None:
    # BLOCKER A: neo4j + embedding configured but MCP_SERVICE_KEY empty → refuse to poll
    # with blank creds (would 401 forever, keeping every runtime pod out of the Service).
    settings = RuntimeSettings(
        _env_file=None,
        neo4j_url="bolt://localhost:7687",
        embedding_api_url="http://e/embed",
        mcp_service_key="",
    )
    with caplog.at_level("ERROR"):
        assert build_hydrator(settings) is None
    assert any("MCP_SERVICE_KEY" in rec.message for rec in caplog.records)


def test_build_hydrator_wires_service_key_clients(monkeypatch) -> None:
    # A recording Neo4jVectorIndex stand-in so no real driver/pool is opened.
    class _Index:
        def __init__(self, **_: Any) -> None:
            self.driver = _Driver()
            self.database = "neo4j"

    monkeypatch.setattr(hydrator_mod, "Neo4jVectorIndex", _Index)
    settings = RuntimeSettings(
        _env_file=None,
        neo4j_url="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="pw",
        embedding_api_url="http://e/embed",
        embedding_model="all-mpnet-base-v2",
        mcp_service_key="svc-key",
    )
    h = build_hydrator(settings)
    assert h is not None
    # Both export clients carry the service key (X-Service-Key mode).
    assert h._catalog_client._auth_headers(jwt="j", session_id="s") == {
        "X-Service-Key": "svc-key"
    }
    assert h._corpus_client._auth_headers(jwt="j", session_id="s") == {
        "X-Service-Key": "svc-key"
    }


def test_build_hydrator_seeds_the_configured_database(monkeypatch) -> None:
    """NEO4J_DATABASE must reach the index the hydrator writes through. The seed and
    the runtime's recall are only "the same graph" because BOTH read this one setting;
    a dropped `database=` here writes the corpus into a database nobody reads."""

    class _Index:
        def __init__(self, **kwargs: Any) -> None:
            self.driver = _Driver()
            # Mirror the real index: the database it was handed is the one it uses.
            self.database = kwargs["database"]

    monkeypatch.setattr(hydrator_mod, "Neo4jVectorIndex", _Index)
    settings = RuntimeSettings(
        _env_file=None,
        neo4j_url="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="pw",
        neo4j_database="reporting",
        embedding_api_url="http://e/embed",
        embedding_model="all-mpnet-base-v2",
        mcp_service_key="svc-key",
    )
    h = build_hydrator(settings)
    assert h is not None
    assert h._database == "reporting"
