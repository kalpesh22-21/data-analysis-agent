"""Layer-1 tests for the governed-corpus export client + process-wide seed cache
(Phase 2). Mirrors `tests/runtime/catalog/test_export_client.py`.

Covers:
  * `FixtureCorpusClient` reads the offline seed YAML and re-keys it by id (the
    fixtures carry NO source/verified — presented as canon downstream).
  * `CorpusCache` fetches ONCE and fires the one-shot seed callback exactly once.
  * Degrade-not-fail: a failing fetch does NOT crash and does NOT cache (retry next
    turn); a raising seed callback re-arms and does not fail the caller.
  * The seed callback is awaited OFF the freeze lock (a slow seed never serializes a
    second concurrent warmer).
  * `build_corpus_cache` selects the fixture vs HTTP client from settings.
  * Neo4j-absent parity: `on_corpus_loaded=None` fires no seed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.retrieval.corpus_client import (
    CorpusCache,
    FixtureCorpusClient,
    HttpCorpusClient,
    build_corpus_cache,
)

_CORPUS_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"


class _CountingClient:
    """A `CorpusClient` double that counts fetches and can be flipped to fail."""

    def __init__(self, export: dict[str, Any], *, fail: bool = False) -> None:
        self._export = export
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        self.calls.append((jwt, session_id))
        if self.fail:
            raise RuntimeError("boom")
        return self._export


class _SlowClient:
    """Awaits once mid-fetch to force the cold-fetch race onto the cache's lock."""

    def __init__(self, export: dict[str, Any]) -> None:
        self._export = export
        self.calls: list[tuple[str, str]] = []

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        self.calls.append((jwt, session_id))
        await asyncio.sleep(0)
        return self._export


_EXPORT = {
    "blueprints": {"bp-1": {"id": "bp-1", "intent": "i", "slots_summary": "", "uses": ["a.b.c"]}},
    "blueprints_sha": "bsha",
    "knowledge": {"kn-1": {"id": "kn-1", "text": "t", "doc_id": "d"}},
    "knowledge_sha": "ksha",
}


# --- FixtureCorpusClient ----------------------------------------------------


async def test_fixture_client_reads_and_rekeys_seed_yaml() -> None:
    client = FixtureCorpusClient(
        _CORPUS_DIR / "blueprints.yaml", _CORPUS_DIR / "knowledge.yaml"
    )
    export = await client.fetch_export(jwt="tok", session_id="s1")
    # Re-keyed by id into the MCP-export {id: entry} shape.
    assert "bp-overtime-by-department" in export["blueprints"]
    assert "kn-overtime-multiplier" in export["knowledge"]
    # The fixtures carry no per-corpus sha; the client computes a stable content hash
    # itself (a non-empty 40-char SHA-1) so `effective_corpus_sha` never warns offline.
    assert len(export["blueprints_sha"]) == 40
    assert len(export["knowledge_sha"]) == 40
    # Deterministic per content — a second read yields the same shas.
    again = await client.fetch_export(jwt="tok", session_id="s2")
    assert again["blueprints_sha"] == export["blueprints_sha"]


async def test_fixture_client_missing_file_raises() -> None:
    from data_agent.runtime.retrieval.corpus_client import CorpusClientError

    client = FixtureCorpusClient("/nonexistent/bp.yaml", "/nonexistent/kn.yaml")
    import pytest

    with pytest.raises(CorpusClientError):
        await client.fetch_export(jwt="tok", session_id="s1")


# --- CorpusCache: fetch-once, seed-once -------------------------------------


async def test_cache_fetches_once_and_seeds_once() -> None:
    seen: list[dict[str, Any]] = []

    async def _seed(export: dict[str, Any]) -> None:
        seen.append(export)

    client = _CountingClient(_EXPORT)
    cache = CorpusCache(client, on_corpus_loaded=_seed)

    assert await cache.ensure_seeded(jwt="t", session_id="s1") is True  # cold
    assert await cache.ensure_seeded(jwt="t", session_id="s2") is True  # warm
    assert await cache.ensure_seeded(jwt="t", session_id="s3") is True  # warm

    assert len(client.calls) == 1  # exactly ONE fetch served all three
    assert len(seen) == 1  # the seed fired exactly once, with the export
    assert seen[0] is _EXPORT
    assert cache.is_loaded is True


async def test_cache_lock_collapses_concurrent_first_turns_to_one_fetch() -> None:
    client = _SlowClient(_EXPORT)
    cache = CorpusCache(client)

    results = await asyncio.gather(
        *(cache.ensure_seeded(jwt=f"t{i}", session_id=f"s{i}") for i in range(20))
    )
    assert all(results)
    assert len(client.calls) == 1  # the lock collapsed the burst to ONE fetch


# --- Degrade-not-fail -------------------------------------------------------


async def test_failed_fetch_does_not_cache_and_retries() -> None:
    client = _CountingClient(_EXPORT, fail=True)
    cache = CorpusCache(client)

    assert await cache.ensure_seeded(jwt="t", session_id="s1") is False  # cold fail
    assert cache.is_loaded is False  # not cached — a later turn retries
    client.fail = False
    assert await cache.ensure_seeded(jwt="t", session_id="s2") is True
    assert len(client.calls) == 2  # failed + recovered


async def test_raising_seed_callback_re_arms_and_does_not_crash() -> None:
    calls: list[dict[str, Any]] = []
    fail = {"on": True}

    async def _seed(export: dict[str, Any]) -> None:
        calls.append(export)
        if fail["on"]:
            raise RuntimeError("neo4j blip")

    client = _CountingClient(_EXPORT)
    cache = CorpusCache(client, on_corpus_loaded=_seed)

    # Cold fetch: the seed raises → degrade (returns False) but never crashes; re-armed.
    assert await cache.ensure_seeded(jwt="t", session_id="s1") is False
    assert len(calls) == 1
    assert cache.is_loaded is False  # re-armed for a retry

    # A later turn re-fetches + retries the seed; now it succeeds.
    fail["on"] = False
    assert await cache.ensure_seeded(jwt="t", session_id="s2") is True
    assert len(calls) == 2
    assert cache.is_loaded is True


async def test_seed_callback_not_awaited_under_the_freeze_lock() -> None:
    gate = asyncio.Event()

    async def _slow(_export: dict[str, Any]) -> None:
        await gate.wait()

    cache = CorpusCache(_SlowClient(_EXPORT), on_corpus_loaded=_slow)

    task_a = asyncio.create_task(cache.ensure_seeded(jwt="tA", session_id="sA"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Turn B must NOT block behind A's parked seed callback — it returns promptly
    # (A already froze the cache + released the lock before entering the callback).
    result_b = await asyncio.wait_for(
        cache.ensure_seeded(jwt="tB", session_id="sB"), timeout=1.0
    )
    assert result_b is True
    assert not task_a.done()  # A still parked in the callback (lock long released)

    gate.set()
    await asyncio.wait_for(task_a, timeout=1.0)


# --- Neo4j-absent parity: no callback => no seed ----------------------------


async def test_no_callback_fires_no_seed_but_still_warms() -> None:
    # When Neo4j is absent app.py passes on_corpus_loaded=None — the cache warms
    # (harmless) but seeds nothing (byte-identical no-op for the corpus feature).
    client = _CountingClient(_EXPORT)
    cache = CorpusCache(client)  # no callback
    assert await cache.ensure_seeded(jwt="t", session_id="s1") is True
    assert cache.is_loaded is True


# --- build_corpus_cache selects the right client ----------------------------


def test_build_corpus_cache_fixture_source() -> None:
    settings = RuntimeSettings(_env_file=None, corpus_source="fixture")
    cache = build_corpus_cache(settings)
    assert isinstance(cache._client, FixtureCorpusClient)


def test_build_corpus_cache_mcp_source_default() -> None:
    settings = RuntimeSettings(_env_file=None)  # default corpus_source="mcp"
    cache = build_corpus_cache(settings)
    assert isinstance(cache._client, HttpCorpusClient)


# --- config: the corpus API base derives the MCP HOST ROOT (not /catalog) ---


def test_corpus_api_base_derives_host_root_from_mcp_url() -> None:
    settings = RuntimeSettings(_env_file=None, mcp_url="http://host:18090/mcp")
    # The two corpus routes live at the host ROOT, NOT under /mcp nor /catalog.
    assert settings.corpus_api_base() == "http://host:18090"


def test_corpus_api_base_honors_explicit_override() -> None:
    settings = RuntimeSettings(
        _env_file=None, corpus_api_url="https://corpus.internal/base/"
    )
    assert settings.corpus_api_base() == "https://corpus.internal/base"
