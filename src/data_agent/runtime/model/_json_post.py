"""The shared skeleton behind the two custom-API JSON POST clients (D71).

Load-bearing in the error ladder: parse + validate happen INSIDE the try, so a
malformed body degrades as the client's own error instead of escaping as a
`KeyError`/`TypeError` past the caller's `except`; and failures record the exception
TYPE, never its text (transport errors carry host/port detail). The two error classes
share no base, so no `except` site can catch both by accident.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    import httpx
    from opentelemetry.trace import Tracer


class JsonPostClient:
    """Constructor, headers, and the traced POST + error ladder for a custom JSON API.

        Subclass contract: `_error_class`, `_failure_prefix` (the transport-failure message
        stem), `_decode(body)` (parse + validate, raising `_error_class`),
        `_count_mismatch_message(got, expected)`, and `_span(count)`.
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
