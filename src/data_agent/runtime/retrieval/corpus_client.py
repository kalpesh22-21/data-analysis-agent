"""CorpusClient — the transport for the MCP corpus exports (governed corpus).

The MCP is the single source of TRUSTED recall canon. Two auth-required routes serve the
git-versioned corpus:

  - `GET /blueprints/export` -> `{"blueprints_sha": <sha>, "blueprints": {<id>: <entry>}}`
  - `GET /knowledge/export`  -> `{"knowledge_sha": <sha>, "knowledge": {<id>: <entry>}}`

Each entry is the verbatim blueprint/knowledge fields PLUS `source="mcp"` + `verified=True`
injected at export time. The singleton hydrator daemon projects these into the neo4j
`source="mcp"` partition — the ONLY partition agent recall serves. A separate
`source="learning"` staging tier is ignored by recall.

`HttpCorpusClient` authenticates with EITHER the static service key (the hydrator's mode, no
user JWT) OR the per-request Bearer/session pair. The corpus is scope-INDEPENDENT, so
credentials authenticate the fetch only and never enter a node or a message (D5).
`FixtureCorpusClient` reads the frozen offline seed YAML instead; those fixtures carry no
`source`/`verified`, and the loader defaults present them as trusted canon.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from data_agent.runtime.mcp._transport import (
    SideChannelError,
    auth_headers,
    error_from_response,
)

_logger = logging.getLogger(__name__)


class CorpusClientError(SideChannelError):
    """A corpus export fetch was rejected or returned an unusable body.

        Carries the endpoint's stable *code* when the JSON error body supplies one. It stays its
        OWN class because the consequence is specific: this one degrades the hydrate, leaving the
        previously-seeded corpus partition in place.
    """


class CorpusClient(Protocol):
    """The corpus-export transport seam (HTTP + fixture share it). Consumed by the
    singleton hydrator daemon (`retrieval/hydrator.py`), which builds an `HttpCorpusClient`
    with the static service key and seeds the neo4j `source='mcp'` recall partition."""

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        """Return the COMBINED corpus export dict:
        `{"blueprints": {...}, "blueprints_sha": ..., "knowledge": {...}, "knowledge_sha": ...}`.

        Raises `CorpusClientError` on a transport/parse failure so the cache degrades.
        """
        ...


def _combined_export(
    blueprints_body: dict[str, Any], knowledge_body: dict[str, Any]
) -> dict[str, Any]:
    """Fuse the two per-corpus export bodies into one dict the loader consumes."""
    return {
        "blueprints": blueprints_body.get("blueprints") or {},
        "blueprints_sha": blueprints_body.get("blueprints_sha"),
        "knowledge": knowledge_body.get("knowledge") or {},
        "knowledge_sha": knowledge_body.get("knowledge_sha"),
    }


class HttpCorpusClient:
    """Real `CorpusClient` over the live MCP corpus-export routes.

        Two auth modes, mirroring `HttpCatalogClient`: the per-request Bearer JWT +
        `X-Session-Id` pair (default), or a static `X-Service-Key` when `service_key=` is set —
        in which case the per-request `jwt`/`session_id` args are IGNORED.
    """

    def __init__(
        self, base_url: str, *, timeout: float = 30.0, service_key: str | None = None
    ) -> None:
        # base_url is the MCP HOST ROOT (no trailing slash); the two route paths are
        # appended. The routes live at the host root, NOT under `/mcp` or `/catalog`.
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._service_key = service_key or None

    def _auth_headers(self, *, jwt: str, session_id: str) -> dict[str, str]:
        """The auth headers for one fetch — the static service key when configured, else the
                per-request Bearer/session pair.
        """
        return auth_headers(service_key=self._service_key, jwt=jwt, session_id=session_id)

    async def _get(
        self, path: str, *, jwt: str, session_id: str, expected_key: str
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}{path}",
                    headers=self._auth_headers(jwt=jwt, session_id=session_id),
                )
        except httpx.HTTPError as exc:
            raise CorpusClientError(None, f"corpus export request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise _error_from_response(resp, path)
        try:
            body = resp.json()
        except ValueError as exc:
            raise CorpusClientError(
                None, f"corpus export {path} returned a non-JSON body"
            ) from exc
        if not isinstance(body, dict) or expected_key not in body:
            raise CorpusClientError(
                None, f"corpus export {path} body missing a '{expected_key}' object"
            )
        return body

    async def fetch_export(self, *, jwt: str = "", session_id: str = "") -> dict[str, Any]:
        blueprints_body = await self._get(
            "/blueprints/export", jwt=jwt, session_id=session_id, expected_key="blueprints"
        )
        knowledge_body = await self._get(
            "/knowledge/export", jwt=jwt, session_id=session_id, expected_key="knowledge"
        )
        return _combined_export(blueprints_body, knowledge_body)


class FixtureCorpusClient:
    """Offline/test `CorpusClient` — reads the frozen seed YAML from disk.

        Credentials are accepted for interface parity but IGNORED. The fixtures carry NO
        `source`/`verified` and no per-corpus sha; the loader defaults present them as trusted
        canon.
    """

    def __init__(self, blueprints_path: Path | str, knowledge_path: Path | str) -> None:
        self._blueprints_path = Path(blueprints_path)
        self._knowledge_path = Path(knowledge_path)

    def _read_keyed(self, path: Path) -> dict[str, Any]:
        """Read a seed YAML LIST and re-key it by each item's `id` into the `{id: entry}`
        shape the MCP export serves. A missing `id` falls back to the list index."""
        try:
            with path.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or []
        except (OSError, ValueError) as exc:
            raise CorpusClientError(
                None, f"corpus fixture at {path} could not be read: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise CorpusClientError(None, f"corpus fixture at {path} must be a YAML list")
        keyed: dict[str, Any] = {}
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            entry_id = str(item.get("id") or index)
            keyed[entry_id] = item
        return keyed

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        blueprints = self._read_keyed(self._blueprints_path)
        knowledge = self._read_keyed(self._knowledge_path)
        # The fixtures have no per-corpus sha, but the client HAS the content — so it
        # computes a stable content hash for each corpus itself. This keeps the B1
        # skip-guard + GC functional offline WITHOUT tripping the
        # `effective_corpus_sha` "lacked a sha" warning on every cold fetch (that
        # warning stays reserved for a genuinely sha-less MCP export).
        return {
            "blueprints": blueprints,
            "blueprints_sha": _content_hash(blueprints),
            "knowledge": knowledge,
            "knowledge_sha": _content_hash(knowledge),
        }


def _content_hash(keyed: dict[str, Any]) -> str:
    """A stable SHA-1 over a `{id: entry}` corpus map (sorted keys) — the offline
    fixture client's stand-in for the MCP's git-versioned per-corpus sha."""
    return hashlib.sha1(
        json.dumps(keyed, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _error_from_response(resp: httpx.Response, path: str) -> SideChannelError:
    return error_from_response(
        resp, error_class=CorpusClientError, description=f"corpus export {path}"
    )


__all__ = [
    "CorpusClient",
    "CorpusClientError",
    "FixtureCorpusClient",
    "HttpCorpusClient",
]
