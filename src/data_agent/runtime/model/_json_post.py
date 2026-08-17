"""The shared skeleton behind the two custom-API JSON POST clients (D71).

`model/embedding_client.py` and `model/reranker_client.py` talk to two different
custom endpoints (NOT the OpenAI SDK) with the same shape: one keyword-only
constructor (url / api key / model / timeout / injectable transport / optional
tracer), an optional bearer header, an empty-batch short-circuit that skips the
network entirely, a manual OpenInference span around the POST when a tracer is
configured, and a fail-closed error ladder that converts EVERY transport, decode
and shape failure into the client's OWN error class so the caller's degrade path
(`except EmbeddingError` -> frequency-only ranking; `except RerankerError` ->
recall order) is the only exit.

That ladder is the reason this is shared rather than copied. Its two load-bearing
properties are easy to lose in a re-typing:

  * **Parse + validate happen INSIDE the try.** A JSON-parseable but malformed
    body must degrade as the client's error, never as a raw `KeyError`/`TypeError`
    that sails past the caller's `except` (the BUG-3 repros).
  * **The wrapper records the exception TYPE, not its text.** Transport errors
    carry host/port detail; `f"...: {type(exc).__name__}"` keeps that out of a
    message that may be surfaced or logged.

Subclasses supply only what genuinely differs: the request payload, the
body-specific parse/validate, the span, and the wording of the count-mismatch
message. The error classes stay per-client and stay plain `Exception` subclasses
— every `except` site names one of them, and they intentionally do NOT share a
base, so no caller can accidentally catch both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    import httpx
    from opentelemetry.trace import Tracer


class JsonPostClient:
    """Constructor, headers, and the traced POST + error ladder for a custom JSON API.

    Subclass contract (all four are required):
      - `_error_class`: the exception every failure is raised as.
      - `_failure_prefix`: the transport-failure message stem, e.g.
        `"Embedding request failed"` -> `"Embedding request failed: TimeoutException"`.
      - `_decode(body)`: parse + validate a 2xx body into the result list, raising
        `_error_class` on any malformed shape or element.
      - `_count_mismatch_message(got, expected)`: the wording used when the
        endpoint returns the wrong number of results.
      - `_span(count)`: the manual OpenInference span wrapping the POST (only
        consulted when a tracer was injected).
    """

    _error_class: ClassVar[type[Exception]]
    _failure_prefix: ClassVar[str]

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

    def _span(self, count: int) -> AbstractContextManager[Any]:
        """The manual span wrapping the POST. Only called when `self._tracer` is set."""
        raise NotImplementedError

    def _decode(self, body: Any) -> list[Any]:
        """Parse + validate a 2xx response body. Raises `_error_class` if malformed."""
        raise NotImplementedError

    def _count_mismatch_message(self, got: int, expected: int) -> str:
        """The message for a well-formed body with the wrong number of results."""
        raise NotImplementedError

    async def _post(self, *, payload: dict[str, Any], expected_count: int) -> list[Any]:
        """POST *payload*, decode the body, and return exactly *expected_count* results.

        Every failure — transport, non-2xx, non-JSON, malformed shape, wrong count —
        leaves as `_error_class`.
        """
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                if self._tracer is not None:
                    with self._span(expected_count):
                        response = await client.post(
                            self._url, json=payload, headers=self._headers()
                        )
                else:
                    response = await client.post(self._url, json=payload, headers=self._headers())
                response.raise_for_status()
                body = response.json()
                # Decode + count check ALL inside the try so a JSON-parseable but
                # malformed body degrades via `_error_class`, never a raw
                # KeyError/TypeError that bypasses the caller's degrade path.
                values = self._decode(body)
                if len(values) != expected_count:
                    raise self._error_class(
                        self._count_mismatch_message(len(values), expected_count)
                    )
        except self._error_class:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport/parse failure degrades
            raise self._error_class(f"{self._failure_prefix}: {type(exc).__name__}") from exc

        return values


__all__ = ["JsonPostClient"]
