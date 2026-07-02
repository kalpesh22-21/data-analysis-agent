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


async def test_http_client_bare_array_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Real contract: request body is {"input_text": [...]} (no model param),
        # response is a BARE JSON array of vectors.
        body = json.loads(request.content)
        assert body == {"input_text": ["hi", "there"]}
        assert request.headers["Authorization"] == "Bearer secret-key"
        return httpx.Response(200, json=[[0.1, 0.2], [0.3, 0.4]])

    client = HttpEmbeddingClient(
        url="https://embeddings.example/embed",
        api_key="secret-key",
        model="test-model",
        transport=_mock_transport(handler),
    )
    vectors = await client.embed(["hi", "there"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


async def test_http_client_dict_body_raises_embedding_error() -> None:
    # An OpenAI-shaped envelope (or any dict) is NOT the contract -> malformed.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[1.0, 2.0]]})

    client = HttpEmbeddingClient(
        url="https://e", api_key="", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a"])


async def test_http_client_non_2xx_raises_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["x"])


async def test_http_client_non_list_body_raises_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json="not a list")

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["x"])


async def test_http_client_count_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[0.1]])  # 1 vec for 2 inputs

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a", "b"])


async def test_http_client_nan_element_raises_embedding_error() -> None:
    # json.loads happily parses NaN; a NaN score must never reach ranking/the
    # model — the client validates finiteness and raises EmbeddingError (H2).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[[NaN, 0.1]]")

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a"])


async def test_http_client_string_element_raises_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[["not", "numbers"]])

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a"])


async def test_http_client_scalar_list_body_raises_embedding_error() -> None:
    # A bare list of scalars ([1, 2]) is not a batch of vectors -> malformed.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a", "b"])


async def test_http_client_mixed_element_row_raises_embedding_error() -> None:
    # [[1, "x"]] — a vector with a non-numeric element -> malformed.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[1, "x"]])

    client = HttpEmbeddingClient(
        url="https://e", api_key="k", model="m", transport=_mock_transport(handler)
    )
    with pytest.raises(EmbeddingError):
        await client.embed(["a"])
