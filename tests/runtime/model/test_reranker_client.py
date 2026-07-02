"""Unit tests for model/reranker_client.py (Layer 1 — Fake + mocked HTTP).

Mirrors test_embedding_client{,_adversarial}.py: happy path, every malformed
shape, timeout / non-2xx / non-JSON / count-mismatch / NaN-inf -> RerankerError
with no raw-exception-text leak, plus the empty-documents short-circuit.
"""

from __future__ import annotations

import json

import httpx
import pytest

from data_agent.runtime.model.reranker_client import (
    FakeRerankerClient,
    HttpRerankerClient,
    RerankerError,
)

# --- FakeRerankerClient ------------------------------------------------------


async def test_fake_scripted_scores_order_preserving() -> None:
    client = FakeRerankerClient({"a": 0.9, "b": 0.1})
    scores = await client.rerank("q", ["a", "b"])
    assert scores == [0.9, 0.1]
    assert client.calls == [("q", ["a", "b"])]


async def test_fake_scripted_zero_score_is_honored_not_replaced() -> None:
    # 0.0 is a valid scripted score; membership (not truthiness) decides.
    client = FakeRerankerClient({"a": 0.0})
    scores = await client.rerank("q", ["a"])
    assert scores == [0.0]


async def test_fake_hash_fallback_deterministic_and_distinct() -> None:
    client = FakeRerankerClient()
    first = await client.rerank("q", ["alpha", "beta"])
    second = await client.rerank("q", ["alpha", "beta"])
    assert first == second
    assert first[0] != first[1]


async def test_fake_fail_raises_reranker_error() -> None:
    client = FakeRerankerClient(fail=True)
    with pytest.raises(RerankerError):
        await client.rerank("q", ["x"])


# --- HttpRerankerClient (mocked transport, no live API) ----------------------


def _client(handler) -> HttpRerankerClient:
    return HttpRerankerClient(
        url="https://r", api_key="k", model="m", transport=httpx.MockTransport(handler)
    )


async def test_http_client_scores_shape_and_request_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"query": "q", "documents": ["a", "b"]}
        assert request.headers["Authorization"] == "Bearer secret-key"
        return httpx.Response(200, json={"scores": [0.3, 0.9]})

    client = HttpRerankerClient(
        url="https://rerank.example/rerank",
        api_key="secret-key",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    assert await client.rerank("q", ["a", "b"]) == [0.3, 0.9]


async def test_http_client_non_2xx_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_4xx_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_timeout_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_connection_error_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_non_json_body_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_raw_exception_text_not_leaked() -> None:
    secret = "internal-host-9000.corp.local"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(secret)

    with pytest.raises(RerankerError) as exc_info:
        await _client(handler).rerank("q", ["x"])
    assert secret not in str(exc_info.value)


# --- malformed-but-JSON bodies ----------------------------------------------


async def test_http_client_bare_list_body_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[0.1, 0.2])

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["a", "b"])


async def test_http_client_missing_scores_key_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [0.1]})

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_scores_not_a_list_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scores": 0.5})

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_count_mismatch_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scores": [0.1]})  # 1 for 2

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["a", "b"])


async def test_http_client_string_score_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scores": ["high"]})

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_bool_score_raises() -> None:
    # bool is an int subclass — a True/False score is a protocol violation.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scores": [True]})

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_nan_score_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"scores": [NaN]}')

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


async def test_http_client_inf_score_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"scores": [Infinity]}')

    with pytest.raises(RerankerError):
        await _client(handler).rerank("q", ["x"])


# --- request wiring ----------------------------------------------------------


async def test_http_client_no_authorization_header_when_key_empty() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"scores": [0.1]})

    client = HttpRerankerClient(
        url="https://r", api_key="", transport=httpx.MockTransport(handler)
    )
    await client.rerank("q", ["x"])
    assert "authorization" not in {k.lower() for k in seen}


async def test_http_client_empty_documents_short_circuits_without_http_call() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200, json={"scores": []})

    assert await _client(handler).rerank("q", []) == []
    assert called is False
