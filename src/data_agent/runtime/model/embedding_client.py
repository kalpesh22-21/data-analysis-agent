"""EmbeddingClient — the D71 embedding seam for `resolveValues` ranking (design §3).

Three pieces:
  - `EmbeddingClient` Protocol: `embed(texts) -> vectors`, order-preserving,
    raising `EmbeddingError` on any transport/serialization failure.
  - `FakeEmbeddingClient` (Layer 1): deterministic vectors from a seeded hash
    of each text, OR an explicit scripted `{text: vector}` map for hand-crafted
    ranking assertions. No network. A `fail=True` flag makes `embed` raise, to
    exercise the composite's degrade-to-freq-only path (design §3.2).
  - `HttpEmbeddingClient` (Layer 2/3, real): a plain `httpx.AsyncClient` POST to
    the custom embedding API (D71 — NOT the OpenAI SDK), settings-driven
    base-URL/key/model, wrapped in a manual `EMBEDDING` span (D24). Raises
    `EmbeddingError` on non-2xx / timeout / malformed body. Its live Layer-2
    test is deferred until the endpoint exists (OQ-1); the request/response
    mapping is unit-tested against a mocked transport.

D5/D25 (load-bearing): no embedding client ever sees `RuntimeCredentials`; the
embedding API key is its OWN secret (from `RuntimeSettings`, `.env`-backed),
distinct from the warehouse JWT. The `EMBEDDING` span logs vector counts and
latency only — never the embedded text.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import httpx
    from opentelemetry.trace import Tracer


class EmbeddingError(Exception):
    """Raised on any embedding transport/serialization failure.

    The `resolveValues` composite catches this and degrades to frequency-only
    ranking (design §3.2) — it never propagates out as a tool-call failure.
    """


class EmbeddingClient(Protocol):
    """The embedding seam `composite/resolve_values.py` depends on."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one dense vector per input text, order-preserving.

        Raises:
            EmbeddingError: on any transport/serialization failure.
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


class HttpEmbeddingClient:
    """Real `EmbeddingClient` — POSTs to the custom embedding API (D71, design §3.1).

    Request body:  `{"model": <model>, "input": [<text>, ...]}`
    Response body: `{"embeddings": [[...], ...]}` (primary) or the OpenAI-shaped
                   `{"data": [{"embedding": [...]}, ...]}` (fallback) — the exact
                   custom-API contract is TBD (OQ-1), so both common shapes are
                   accepted; anything else is a malformed body -> `EmbeddingError`.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._tracer = tracer

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _parse_vectors(body: Any) -> list[Any]:
        """Pull the raw per-input vector list out of a response body.

        Raises `EmbeddingError` on any structurally-malformed body — the
        element CONTENTS are validated separately by `_validate_vectors`.
        """
        if isinstance(body, dict):
            if isinstance(body.get("embeddings"), list):
                return list(body["embeddings"])
            data = body.get("data")
            if isinstance(data, list):
                out: list[Any] = []
                for item in data:
                    if not isinstance(item, dict) or "embedding" not in item:
                        raise EmbeddingError("Malformed embedding item in 'data'.")
                    out.append(item["embedding"])
                return out
        raise EmbeddingError("Malformed embedding API response body.")

    @staticmethod
    def _validate_vectors(vectors: list[Any]) -> list[list[float]]:
        """Validate every vector is a non-empty list of finite real numbers.

        Guards the composite's ranking path from later `TypeError`s (string
        elements) and from NaN/inf `score`s (which `json.loads` happily parses)
        being persisted to `result_full` / shown to the model. Raises
        `EmbeddingError` on any violation so the composite degrades cleanly.
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
        import httpx

        payload = {"model": self._model, "input": list(texts)}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                if self._tracer is not None:
                    from data_agent.runtime.observability import tracing

                    with tracing.embedding_span(
                        self._tracer, model=self._model, input_count=len(texts)
                    ):
                        response = await client.post(
                            self._url, json=payload, headers=self._headers()
                        )
                else:
                    response = await client.post(
                        self._url, json=payload, headers=self._headers()
                    )
                response.raise_for_status()
                body = response.json()
                # Parse + shape/count/content validation ALL inside the try so a
                # JSON-parseable-but-malformed body degrades via EmbeddingError,
                # never a raw KeyError/TypeError that bypasses the composite's
                # degrade path (which catches only EmbeddingError).
                vectors = self._validate_vectors(self._parse_vectors(body))
                if len(vectors) != len(texts):
                    raise EmbeddingError(
                        f"Embedding API returned {len(vectors)} vectors "
                        f"for {len(texts)} inputs."
                    )
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport/parse failure degrades
            raise EmbeddingError(f"Embedding request failed: {type(exc).__name__}") from exc

        return vectors


__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "FakeEmbeddingClient",
    "HttpEmbeddingClient",
]
