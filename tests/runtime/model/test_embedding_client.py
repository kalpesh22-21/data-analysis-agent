"""Unit tests for model/embedding_client.py (Layer 1 — Fake + mocked HTTP)."""

from __future__ import annotations

import json

import httpx
import pytest

from data_agent.runtime.model.embedding_client import (
    EmbeddingError,
    FakeEmbeddingClient,
    HttpEmbeddingClient,
)

# --- FakeEmbeddingClient -----------------------------------------------------


async def test_fake_scripted_vectors_order_preserving() -> None:
    client = FakeEmbeddingClient({"a": [1.0, 0.0], "b": [0.0, 1.0]}, dim=2)
    vectors = await client.embed(["a", "b"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert client.calls == [["a", "b"]]


async def test_fake_hash_vectors_deterministic() -> None:
    client = FakeEmbeddingClient(dim=4)
    first = await client.embed(["hello"])
    second = await client.embed(["hello"])
    assert first == second
    assert len(first[0]) == 4


async def test_fake_fail_raises_embedding_error() -> None:
    client = FakeEmbeddingClient(fail=True)
    with pytest.raises(EmbeddingError):
        await client.embed(["x"])


# --- HttpEmbeddingClient (mocked transport, no live API) ---------------------


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_http_client_embeddings_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"model": "test-model", "input": ["hi", "there"]}
        assert request.headers["Authorization"] == "Bearer secret-key"
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    client = HttpEmbeddingClient(
        url="https://embeddings.example/v1/embed",
        api_key="secret-key",
        model="test-model",
        transport=_mock_transport(handler),
    )
    vectors = await client.embed(["hi", "there"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


async def test_http_client_openai_data_shape_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"embedding": [1.0, 2.0]}, {"embedding": [3.0, 4.0]}]}
        )

    client = HttpEmbeddingClient(
        url="https://e", api_key="", model="m", transport=_mock_transport(handler)
    )
    assert await client.embed(["a", "b"]) == [[1.0, 2.0], [3.0, 4.0]]


async def test_http_client_non_2xx_raises_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["x"])


async def test_http_client_malformed_body_raises_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["x"])


async def test_http_client_count_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1]]})  # 1 vec for 2 inputs

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a", "b"])


async def test_http_client_nan_element_raises_embedding_error() -> None:
    # json.loads happily parses NaN; a NaN score must never reach ranking/the
    # model — the client validates finiteness and raises EmbeddingError (H2).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"embeddings": [[NaN, 0.1]]}')

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a"])


async def test_http_client_string_element_raises_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [["not", "numbers"]]})

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a"])
