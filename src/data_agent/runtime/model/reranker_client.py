"""RerankerClient — the D71 reranker seam (retrieval pipeline building block).

Mirrors `model/embedding_client.py` exactly (same structure + hardening), for
the custom cross-encoder reranker API (D71 — NOT the OpenAI SDK). Three pieces:
  - `RerankerClient` Protocol: `rerank(query, documents) -> scores`, one score
    per document, SAME order as the input (higher = more relevant; the caller
    re-sorts). Raises `RerankerError` on any transport/serialization failure.
  - `FakeRerankerClient` (Layer 1): scripted `{document: score}` scores (with a
    deterministic seeded-hash fallback for unscripted documents), OR a
    `fail=True` flag that makes `rerank` raise — for exercising a caller's
    degrade path. No network.
  - `HttpRerankerClient` (Layer 2/3, real): a plain `httpx.AsyncClient` POST,
    settings-driven URL/key, wrapped in a manual `RERANKER` span (D24). Raises
    `RerankerError` on non-2xx / timeout / malformed body.

Wire contract (resolved against the SQL-repo mocks at ~/Development/SQL/mocks):
    request  : POST <url>  {"query": <str>, "documents": [<str>, ...]}
    response : {"scores": [<float>, ...]}   (one score per document, same order)
The mock needs no auth; `api_key` is kept as an optional bearer header for the
eventual production endpoint. An empty `documents` list returns `[]` WITHOUT a
network call (matching the mock, which returns `{"scores": []}` for empty input).

NOT WIRED YET: this is a standalone building block for the upcoming retrieval-
pipeline brick (D71/§3 retrieval). It is intentionally NOT constructed in
`app.py` or dispatched in the agent loop — a later brick composes it into the
blueprint/knowledge retrieval path.

D5/D25 (load-bearing): the reranker client never sees `RuntimeCredentials`; the
reranker API key is its OWN secret (from `RuntimeSettings`, `.env`-backed). The
`RERANKER` span logs document counts + model id + latency only — never the
query or the document text.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from ._json_post import JsonPostClient

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    import httpx
    from opentelemetry.trace import Tracer


class RerankerError(Exception):
    """Raised on any reranker transport/serialization failure.

    The retrieval caller catches this and degrades (e.g. falls back to the
    pre-rerank recall order) — it never propagates out as a hard failure.
    """


class RerankerClient(Protocol):
    """The reranker seam the retrieval pipeline depends on."""

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Return one relevance score per document, order-preserving.

        Higher score = more relevant; the caller re-sorts. Raises:
            RerankerError: on any transport/serialization failure.
        """
        ...


def _hash_score(query: str, document: str) -> float:
    """A deterministic, reproducible pseudo-score for (query, document)."""
    digest = hashlib.sha256(f"{query}\x00{document}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


class FakeRerankerClient:
    """Layer-1 `RerankerClient` double — scripted or seeded-hash scores."""

    def __init__(
        self,
        scores: dict[str, float] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self._scores = dict(scores or {})
        self._fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        if self._fail:
            raise RerankerError("FakeRerankerClient scripted to fail.")
        # Explicit membership check so a scripted score of 0.0 is honored (not
        # treated as "unscripted" and replaced by the hash fallback).
        return [
            self._scores[doc] if doc in self._scores else _hash_score(query, doc)
            for doc in documents
        ]


class HttpRerankerClient(JsonPostClient):
    """Real `RerankerClient` — POSTs to the custom reranker API (D71).

    Wire contract (resolved against the SQL-repo mocks):
        Request body:  `{"query": <str>, "documents": [<str>, ...]}`
        Response body: `{"scores": [<float>, ...]}` — one score per document,
                       order-preserving. Anything else (a non-dict body, a
                       missing/non-list `scores`) is malformed -> `RerankerError`.

    `model` is not a request parameter (the endpoint serves a single fixed
    cross-encoder) — it is retained solely as the RERANKER span's
    `reranker.model` attribute so traces stay coherent (D24).

    The constructor, the bearer header, the traced POST and the fail-closed error
    ladder come from `JsonPostClient`; only the payload, the `{"scores": [...]}`
    parse and the span differ from the embedding client. `__init__` is restated
    solely to keep `model` OPTIONAL here (the reranker span tolerates an unnamed
    model; the embedding client requires one).
    """

    _error_class: ClassVar[type[Exception]] = RerankerError
    _failure_prefix: ClassVar[str] = "Rerank request failed"

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str = "",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        super().__init__(
            url=url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            transport=transport,
            tracer=tracer,
        )

    def _span(self, count: int) -> AbstractContextManager[Any]:
        from data_agent.runtime.observability import tracing

        return tracing.rerank_span(self._tracer, model=self._model, document_count=count)

    def _decode(self, body: Any) -> list[float]:
        return self._validate_scores(self._parse_scores(body))

    def _count_mismatch_message(self, got: int, expected: int) -> str:
        return f"Reranker API returned {got} scores for {expected} documents."

    @staticmethod
    def _parse_scores(body: Any) -> list[Any]:
        """Return the raw `scores` list from a `{"scores": [...]}` response body.

        Raises `RerankerError` on a structurally-malformed body — the element
        CONTENTS are validated separately by `_validate_scores`.
        """
        if isinstance(body, dict) and isinstance(body.get("scores"), list):
            return list(body["scores"])
        raise RerankerError("Malformed reranker API response body.")

    @staticmethod
    def _validate_scores(scores: list[Any]) -> list[float]:
        """Validate every score is a finite real number (no bool).

        Guards the caller's re-sort from `TypeError`s (string scores) and from
        NaN/inf scores (which `json.loads` happily parses) corrupting the sort.
        Raises `RerankerError` on any violation.
        """
        normalized: list[float] = []
        for element in scores:
            if (
                isinstance(element, bool)
                or not isinstance(element, int | float)
                or not math.isfinite(element)
            ):
                raise RerankerError("Reranker score is non-finite or non-numeric.")
            normalized.append(float(element))
        return normalized

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        # Empty documents -> [] with NO network call (matches the mock, which
        # returns {"scores": []} for an empty batch).
        if not documents:
            return []

        return await self._post(
            payload={"query": query, "documents": list(documents)},
            expected_count=len(documents),
        )


__all__ = [
    "FakeRerankerClient",
    "HttpRerankerClient",
    "RerankerClient",
    "RerankerError",
]
