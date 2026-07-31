"""Layer-1 tests for the MCP catalog-export client + process-wide cache (D75 Wave 1b).

Covers:
  * `FixtureCatalogClient` loads the frozen export from disk (offline/test mode).
  * `CatalogCache` fetches ONCE and serves every subsequent turn/scope until reload
    (mirrors the `ToolSchemaCache` cache-until-reload contract).
  * `CatalogCache` fails CLOSED: a failing fetch with no warm cache yields an EMPTY
    handle (no crash), and a later successful fetch recovers.
  * The built `CatalogHandle.schema` / description-cols and the `SemanticCatalogHandle`
    grain match the fixture's authored values.
  * `build_catalog_cache` selects the fixture vs HTTP client from settings.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from data_agent.runtime.catalog.export_client import (
    CatalogCache,
    CatalogClientError,
    FixtureCatalogClient,
    HttpCatalogClient,
    build_catalog_cache,
)
from data_agent.runtime.config import RuntimeSettings
from tests._catalog_fixture import CATALOG_EXPORT_PATH, load_catalog_export

_E = "dbpcm_warehouse.employee"


class _CountingClient:
    """A `CatalogClient` double that counts fetches and can be flipped to fail."""

    def __init__(self, export: dict[str, Any], *, fail: bool = False) -> None:
        self._export = export
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        self.calls.append((jwt, session_id))
        if self.fail:
            raise CatalogClientError(None, "boom")
        return self._export


class _SlowCountingClient:
    """A `CatalogClient` double that awaits once mid-fetch to force the cold-fetch
    race, then counts every invocation. The `await asyncio.sleep(0)` yields control
    so all gathered first-turns pile up on the cache's lock before the first fetch
    resolves — exposing any freeze that let a second fetch through."""

    def __init__(self, export: dict[str, Any]) -> None:
        self._export = export
        self.calls: list[tuple[str, str]] = []

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        self.calls.append((jwt, session_id))
        await asyncio.sleep(0)
        return self._export


# --- FixtureCatalogClient ---------------------------------------------------


async def test_fixture_client_loads_frozen_export() -> None:
    client = FixtureCatalogClient(CATALOG_EXPORT_PATH)
    export = await client.fetch_export(jwt="tok", session_id="s1")
    assert "catalog" in export
    assert _E in export["catalog"]


async def test_fixture_client_missing_file_raises() -> None:
    client = FixtureCatalogClient("/nonexistent/catalog_export.json")
    with pytest.raises(CatalogClientError):
        await client.fetch_export(jwt="tok", session_id="s1")


# --- CatalogCache: fetch-once, serve-everywhere -----------------------------


async def test_cache_fetches_once_across_scopes() -> None:
    client = _CountingClient(load_catalog_export())
    cache = CatalogCache(client)

    first = await cache.get_catalog_handle(jwt="tok", session_id="s1")
    # A different turn / session / scope reuses the first fetch (scope-independent).
    second = await cache.get_catalog_handle(jwt="other-tok", session_id="s2")
    semantic = await cache.get_semantic_catalog_handle(jwt="tok3", session_id="s3")

    assert first is second  # same frozen handle object
    assert semantic is not None
    assert len(client.calls) == 1  # exactly ONE fetch served all three calls


async def test_cache_force_reload_refetches() -> None:
    client = _CountingClient(load_catalog_export())
    cache = CatalogCache(client)

    await cache.get_catalog_handle(jwt="tok", session_id="s1")
    await cache.get_catalog_handle(jwt="tok", session_id="s1", force_reload=True)
    assert len(client.calls) == 2


async def test_cache_lock_freezes_first_build_under_concurrent_first_turns() -> None:
    """Concurrent cold first-turns issue EXACTLY ONE fetch and share ONE frozen handle.

    The `asyncio.Lock` around the cold-fetch-and-freeze (double-checked inside the
    lock) means a burst of first-turns racing the empty cache serialize on it: the
    first successful build wins and every later racer reuses that frozen handle,
    rather than a slower in-flight fetch replacing an already-frozen one. Regression
    guard for the freeze the cache relies on (the catalog drives provenance/scope, so
    a mid-race swap would be security-adjacent)."""
    client = _SlowCountingClient(load_catalog_export())
    cache = CatalogCache(client)

    # 20 first-turns (distinct creds) race the cold fetch simultaneously.
    handles = await asyncio.gather(
        *(cache.get_catalog_handle(jwt=f"tok{i}", session_id=f"s{i}") for i in range(20))
    )

    assert len(client.calls) == 1  # the lock collapsed the burst to ONE fetch
    # Every racer got the SAME frozen handle object (first build wins).
    assert all(handle is handles[0] for handle in handles)
    assert handles[0].is_catalogued("dbpcm_warehouse", "employee")


# --- Fail-closed semantics --------------------------------------------------


async def test_cache_fail_closed_returns_empty_handle_without_caching() -> None:
    client = _CountingClient(load_catalog_export(), fail=True)
    cache = CatalogCache(client)

    handle = await cache.get_catalog_handle(jwt="tok", session_id="s1")
    # Empty (fail-closed) handle — no columns, so provenance capture degrades to
    # undetermined and the trail entry is dropped from replay (D44).
    assert handle.schema == {}
    assert not handle.is_catalogued("dbpcm_warehouse", "employee")

    # The empty handle was NOT cached: the next turn retries. Flip the client to
    # succeed and the cache recovers.
    client.fail = False
    recovered = await cache.get_catalog_handle(jwt="tok", session_id="s2")
    assert recovered.is_catalogued("dbpcm_warehouse", "employee")
    assert len(client.calls) == 2  # first (failed) + second (succeeded)


async def test_cache_serves_warm_handle_when_later_fetch_fails() -> None:
    client = _CountingClient(load_catalog_export())
    cache = CatalogCache(client)

    warm = await cache.get_catalog_handle(jwt="tok", session_id="s1")
    # A later forced reload fails — the still-warm cached handle keeps serving.
    client.fail = True
    still = await cache.get_catalog_handle(jwt="tok", session_id="s2", force_reload=True)
    assert still is warm


# --- Built handles match the fixture's authored values ----------------------


async def test_built_handles_match_fixture_values() -> None:
    cache = CatalogCache(FixtureCatalogClient(CATALOG_EXPORT_PATH))

    catalog_handle = await cache.get_catalog_handle(jwt="tok", session_id="s1")
    semantic_handle = await cache.get_semantic_catalog_handle(jwt="tok", session_id="s1")

    # Schema carries the employee columns + their type strings.
    emp_cols = catalog_handle.schema[_E]
    assert emp_cols["EmployeeStatus"] == "Nullable(String)"
    assert emp_cols["AnnualSalary"] == "Nullable(Decimal(18, 6))"

    # Description-col linkage survives the export round-trip (payroll TypeCode).
    assert (
        catalog_handle.description_col_for("dbpcm_warehouse.payroll", "TypeCode")
        == "TypeCodeDescription"
    )

    # Semantic grain view: employee verifiable, payroll not.
    assert semantic_handle.is_grain_verifiable(_E) is True
    assert semantic_handle.is_grain_verifiable("dbpcm_warehouse.payroll") is False


# --- build_catalog_cache selects the right client ---------------------------


def test_build_catalog_cache_fixture_source() -> None:
    settings = RuntimeSettings(catalog_source="fixture")
    cache = build_catalog_cache(settings)
    assert isinstance(cache._client, FixtureCatalogClient)


def test_build_catalog_cache_mcp_source_default() -> None:
    settings = RuntimeSettings()  # default catalog_source="mcp"
    cache = build_catalog_cache(settings)
    assert isinstance(cache._client, HttpCatalogClient)
