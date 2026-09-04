from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx


class HelpCenterError(Exception):
    pass


@dataclass(frozen=True)
class HelpCenterSearchHit:
    id: str
    score: float
    snippet: str


@dataclass(frozen=True)
class HelpCenterDocument:
    id: str
    content: str


class HelpCenterClient(Protocol):
    async def search(self, query: str, limit: int) -> list[HelpCenterSearchHit]: ...

    async def get_document(self, article_id: str) -> HelpCenterDocument | None: ...


class HttpHelpCenterClient:
    def __init__(
        self,
        *,
        search_url: str,
        documents_url: str,
        api_key: str = "",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._search_url = search_url
        self._documents_url = documents_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def search(self, query: str, limit: int) -> list[HelpCenterSearchHit]:
        body = await self._request("POST", self._search_url, json={"query": query, "limit": limit})
        raw_hits = body.get("documents") if isinstance(body, dict) else None
        if not isinstance(raw_hits, list):
            raise HelpCenterError("Malformed Help Center search response.")
        hits: list[HelpCenterSearchHit] = []
        for raw in raw_hits:
            if not isinstance(raw, dict):
                raise HelpCenterError("Malformed Help Center search result.")
            article_id, score, snippet = raw.get("id"), raw.get("score"), raw.get("snippet")
            if (
                not isinstance(article_id, str)
                or not article_id.strip()
                or isinstance(score, bool)
                or not isinstance(score, int | float)
                or not math.isfinite(score)
                or not isinstance(snippet, str)
                or not snippet.strip()
            ):
                raise HelpCenterError("Malformed Help Center search result.")
            hits.append(HelpCenterSearchHit(article_id, float(score), snippet))
        return hits

    async def get_document(self, article_id: str) -> HelpCenterDocument | None:
        url = f"{self._documents_url}/{quote(article_id, safe='')}"
        try:
            body = await self._request("GET", url)
        except HelpCenterError as exc:
            if exc.args == ("not_found",):
                return None
            raise
        if not isinstance(body, dict):
            raise HelpCenterError("Malformed Help Center document response.")
        returned_id, content = body.get("id"), body.get("content")
        if returned_id != article_id or not isinstance(content, str) or not content.strip():
            raise HelpCenterError("Malformed Help Center document response.")
        return HelpCenterDocument(id=returned_id, content=content)

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = await client.request(method, url, headers=self._headers(), **kwargs)
                if response.status_code == 404:
                    raise HelpCenterError("not_found")
                response.raise_for_status()
                return response.json()
        except HelpCenterError:
            raise
        except Exception as exc:
            raise HelpCenterError(f"Help Center request failed: {type(exc).__name__}") from exc


__all__ = [
    "HelpCenterClient",
    "HelpCenterDocument",
    "HelpCenterError",
    "HelpCenterSearchHit",
    "HttpHelpCenterClient",
]
