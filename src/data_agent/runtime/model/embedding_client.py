"""EmbeddingClient — the D71 embedding seam for `resolveValues` ranking.

Wire contract of the custom endpoint (NOT the OpenAI SDK):
    request  : POST <url>  {"input_text": [<text>, ...]}
    response : a BARE JSON array `[[float, ...], ...]`, one vector per input
D5/D25: no embedding client ever sees `RuntimeCredentials` — the embedding API key is
its own secret, and the `EMBEDDING` span logs counts and latency, never the text.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from ._json_post import JsonPostClient

if TYPE_CHECKING:
    from contextlib import AbstractContextManager


class EmbeddingError(Exception):
    """Raised on any embedding transport/serialization failure.

        `resolveValues` catches this and degrades to frequency-only ranking; it never
        propagates out as a tool-call failure.
    """


class EmbeddingClient(Protocol):
    """The embedding seam `composite/resolve_values.py` depends on."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one dense vector per input text, order-preserving.

                Raises `EmbeddingError` on any transport/serialization failure.
        """
        ...


def _hash_vector(text: str, dim: int) -> list[float]:
    """A deterministic, reproducible pseudo-vector for *text* (Layer-1 only)."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    # Repeat the digest if a longer vector is requested than 32 bytes.
    raw = (digest * ((dim // len(digest)) + 1))[:dim]
    return [byte / 255.0 for byte in raw]


class FakeEmbeddingClient:
    """Layer-1 `EmbeddingClient` double — scripted or seeded-hash vectors."""

    def __init__(
        self,
        vectors: dict[str, list[float]] | None = None,
        *,
        dim: int = 16,
        fail: bool = False,
    ) -> None:
        self._vectors = dict(vectors or {})
        self._dim = dim
        self._fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._fail:
            raise EmbeddingError("FakeEmbeddingClient scripted to fail.")
        return [self._vectors.get(text) or _hash_vector(text, self._dim) for text in texts]


class HttpEmbeddingClient(JsonPostClient):
    """Real `EmbeddingClient` — POSTs to the custom embedding API (D71).

        The response must be a BARE JSON array `[[float, ...], ...]`, one vector per input,
        order-preserving; anything else is malformed -> `EmbeddingError`. `model` is not a
        request parameter (the endpoint serves one fixed model) — it is retained only as
        the EMBEDDING span's `embedding.model` attribute.
    """

    _error_class: ClassVar[type[Exception]] = EmbeddingError
    _failure_prefix: ClassVar[str] = "Embedding request failed"

    def _span(self, count: int) -> AbstractContextManager[Any]:
        from data_agent.runtime.observability import tracing

        return tracing.embedding_span(self._tracer, model=self._model, input_count=count)

    def _decode(self, body: Any) -> list[list[float]]:
        return self._validate_vectors(self._parse_vectors(body))

    def _count_mismatch_message(self, got: int, expected: int) -> str:
        return f"Embedding API returned {got} vectors for {expected} inputs."

    @staticmethod
    def _parse_vectors(body: Any) -> list[Any]:
        """Return the raw per-input vector list from a bare-array response body.

                Raises `EmbeddingError` on a structurally-malformed body; element CONTENTS are
                validated separately by `_validate_vectors`.
        """
        if not isinstance(body, list):
            raise EmbeddingError("Malformed embedding API response body (expected a JSON array).")
        return body

    @staticmethod
    def _validate_vectors(vectors: list[Any]) -> list[list[float]]:
        """Validate every vector is a non-empty list of finite real numbers.

                NaN/inf parse cleanly through `json.loads` but corrupt ranking and would be
                persisted to `result_full`, so they are rejected here as `EmbeddingError`.
        """
        normalized: list[list[float]] = []
        for vec in vectors:
            if not isinstance(vec, list | tuple) or len(vec) == 0:
                raise EmbeddingError("Embedding vector is not a non-empty list.")
            row: list[float] = []
            for element in vec:
                if (
                    isinstance(element, bool)
                    or not isinstance(element, int | float)
                    or not math.isfinite(element)
                ):
                    raise EmbeddingError(
                        "Embedding vector contains a non-finite or non-numeric value."
                    )
                row.append(float(element))
            normalized.append(row)
        return normalized

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Empty input -> [] with NO network call (matches the mock, which returns
        # [] for an empty batch; also keeps the count check below trivially true).
        if not texts:
            return []

        return await self._post(
            payload={"input_text": list(texts)}, expected_count=len(texts)
        )


__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "FakeEmbeddingClient",
    "HttpEmbeddingClient",
]
