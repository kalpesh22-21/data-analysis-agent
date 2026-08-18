"""RerankerClient — the D71 reranker seam for the retrieval pipeline.

Wire contract of the custom cross-encoder endpoint (NOT the OpenAI SDK):
    request  : POST <url>  {"query": <str>, "documents": [<str>, ...]}
    response : {"scores": [<float>, ...]}   (one score per document, same order)
An empty `documents` list returns `[]` without a network call. D5/D25: the client never
sees `RuntimeCredentials`, and the `RERANKER` span logs counts, model id and latency
only — never the query or the document text.
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

        The retrieval caller catches this and degrades to the pre-rerank recall order.
    """


class RerankerClient(Protocol):
    """The reranker seam the retrieval pipeline depends on."""

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Return one relevance score per document, order-preserving.

                Higher = more relevant; the caller re-sorts. Raises `RerankerError` on any
                transport/serialization failure.
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

        The response must be `{"scores": [<float>, ...]}`, one score per document,
        order-preserving; anything else is malformed -> `RerankerError`. `model` is not a
        request parameter — it is retained only as the RERANKER span's `reranker.model`
        attribute, and stays OPTIONAL here (the embedding client requires one).
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

                Raises `RerankerError` on a structurally-malformed body; element CONTENTS are
                validated separately by `_validate_scores`.
        """
        if isinstance(body, dict) and isinstance(body.get("scores"), list):
            return list(body["scores"])
        raise RerankerError("Malformed reranker API response body.")

    @staticmethod
    def _validate_scores(scores: list[Any]) -> list[float]:
        """Validate every score is a finite real number (no bool).

                NaN/inf parse cleanly through `json.loads` but corrupt the caller's re-sort,
                so they are rejected here as `RerankerError`.
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
