"""Layer-1 tests for the governed-corpus export client (Phase 2).

The per-turn `CorpusCache` was removed in the singleton-hydrator redesign (the hydrator
daemon owns the seed loop and builds an `HttpCorpusClient` directly). What remains is the
transport: the fixture reader + the config-derived host root. Service-key auth mode is
covered in `tests/runtime/test_service_key_clients.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_agent.runtime.config import RuntimeSettings
from data_agent.runtime.retrieval.corpus_client import (
    CorpusClientError,
    FixtureCorpusClient,
)

_CORPUS_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"


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
    client = FixtureCorpusClient("/nonexistent/bp.yaml", "/nonexistent/kn.yaml")
    with pytest.raises(CorpusClientError):
        await client.fetch_export(jwt="tok", session_id="s1")


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
