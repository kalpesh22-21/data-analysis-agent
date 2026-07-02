"""Adversarial Layer-1 tests for model/embedding_client.py (D77, design §3).

Covers HttpEmbeddingClient failure modes the happy-path suite misses: request
timeout, connection error, non-JSON body, and — critically — malformed but
JSON-parseable bodies in each accepted shape.

The `test_data_items_*` / `test_embeddings_entries_*` cases were the BUG-3
xfail repros: `_parse_vectors` + validation now run INSIDE `embed()`'s
try/except, so a malformed but JSON-parseable body raises the contract's
`EmbeddingError` (not a raw `KeyError`/`TypeError`) and the composite degrades
to freq-only. They pass as regular tests now.
"""

from __future__ import annotations

import httpx
import pytest

from data_agent.runtime.model.embedding_client import (
    EmbeddingError,
    FakeEmbeddingClient,
    HttpEmbeddingClient,
)


def _client(handler) -> HttpEmbeddingClient:
    return HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=httpx.MockTransport(handler)
    )


# --- transport failures (correctly wrapped) ---------------------------------


async def test_timeout_raises_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


async def test_connection_error_raises_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


async def test_non_json_body_raises_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


async def test_raw_exception_text_not_leaked_in_embedding_error() -> None:
    secret = "internal-host-9000.corp.local"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(secret)

    with pytest.raises(EmbeddingError) as exc_info:
        await _client(handler).embed(["x"])
    # The wrapper records the exception TYPE, not the raw message text.
    assert secret not in str(exc_info.value)


async def test_4xx_raises_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


# --- count-mismatch on the data shape too -----------------------------------


async def test_data_shape_count_mismatch_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})  # 1 for 2

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["a", "b"])


async def test_empty_embeddings_list_for_nonempty_input_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": []})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["a"])


# --- malformed-but-JSON bodies: BUG-3 repros --------------------------------


async def test_data_items_missing_embedding_key_raise_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"vector": [0.1, 0.2]}]})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


async def test_data_items_not_dicts_raise_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": ["not-a-dict"]})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


async def test_embeddings_entries_not_iterable_raise_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [3.0]})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


# --- request wiring ---------------------------------------------------------


async def test_no_authorization_header_when_key_empty() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"embeddings": [[0.1]]})

    client = HttpEmbeddingClient(
        url="https://e", api_key="", model="m", transport=httpx.MockTransport(handler)
    )
    await client.embed(["x"])
    assert "authorization" not in {k.lower() for k in seen}


async def test_empty_input_list_round_trips() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": []})

    assert await _client(handler).embed([]) == []


# --- FakeEmbeddingClient determinism / distinctness -------------------------


async def test_fake_distinct_texts_get_distinct_vectors() -> None:
    client = FakeEmbeddingClient(dim=8)
    [va, vb] = await client.embed(["alpha", "beta"])
    assert va != vb


async def test_fake_scripted_falls_back_to_hash_for_unknown_text() -> None:
    client = FakeEmbeddingClient({"known": [1.0, 0.0]}, dim=2)
    vectors = await client.embed(["known", "unknown"])
    assert vectors[0] == [1.0, 0.0]
    assert len(vectors[1]) == 2  # hash fallback, not a crash
