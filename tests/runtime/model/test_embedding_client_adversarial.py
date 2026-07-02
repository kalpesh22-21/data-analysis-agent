"""Adversarial Layer-1 tests for model/embedding_client.py (D77, design §3).

Covers HttpEmbeddingClient failure modes the happy-path suite misses: request
timeout, connection error, non-JSON body, and — critically — malformed but
JSON-parseable bodies for the real BARE-ARRAY contract (OQ-1 resolved:
`{"input_text": [...]}` -> `[[float, ...], ...]`).

The malformed-body cases were the BUG-3 xfail repros: `_parse_vectors` +
validation now run INSIDE `embed()`'s try/except, so a malformed but
JSON-parseable body raises the contract's `EmbeddingError` (not a raw
`KeyError`/`TypeError`) and the composite degrades to freq-only. They pass as
regular tests now.
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


# --- count mismatch on the bare-array shape ---------------------------------


async def test_count_mismatch_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[0.1]])  # 1 vec for 2 inputs

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["a", "b"])


async def test_empty_array_for_nonempty_input_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["a"])


# --- malformed-but-JSON bodies: BUG-3 repros --------------------------------


async def test_dict_body_raises_embedding_error() -> None:
    # An OpenAI-shaped envelope (the OLD assumed contract) is now malformed.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


async def test_scalar_list_body_raises_embedding_error() -> None:
    # [3.0] — a bare list of scalars, not a batch of vectors.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[3.0])

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


async def test_row_with_nested_non_numeric_raises_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[[0.1]]])  # vector element is itself a list

    with pytest.raises(EmbeddingError):
        await _client(handler).embed(["x"])


# --- request wiring ---------------------------------------------------------


async def test_request_body_uses_input_text_key() -> None:
    import json

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[[0.1]])

    await _client(handler).embed(["x"])
    assert seen["body"] == {"input_text": ["x"]}


async def test_no_authorization_header_when_key_empty() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[[0.1]])

    client = HttpEmbeddingClient(
        url="https://e", api_key="", model="m", transport=httpx.MockTransport(handler)
    )
    await client.embed(["x"])
    assert "authorization" not in {k.lower() for k in seen}


async def test_empty_input_list_short_circuits_without_http_call() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    assert await _client(handler).embed([]) == []
    assert called is False


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
