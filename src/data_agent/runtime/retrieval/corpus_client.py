"""CorpusClient — the transport for the MCP corpus exports (governed corpus, Phase 2).

The MCP is the single source of TRUSTED recall canon. Two auth-required routes serve
the git-versioned corpus:

  - `GET /blueprints/export` → `{"blueprints_sha": <sha>, "blueprints": {<id>: <entry>}}`
  - `GET /knowledge/export`  → `{"knowledge_sha": <sha>, "knowledge": {<id>: <entry>}}`

Each entry is the verbatim blueprint/knowledge fields PLUS `source="mcp"` +
`verified=True` injected at export time. The singleton hydrator daemon
(`retrieval/hydrator.py`) projects these into the neo4j recall corpus as the
`source="mcp"` partition — the ONLY partition the agent recall serves (the trust gate in
`vector_index`). A separate learning-staging tier (`source="learning"`) recall ignores.

`CorpusClient` (Protocol) has two implementations:
  * `HttpCorpusClient`    — `GET {root}/blueprints/export` + `GET {root}/knowledge/export`
    on the MCP host. Auth is EITHER the static service key (`X-Service-Key`, the hydrator's
    mode — no user JWT) OR the per-request `Authorization: Bearer <jwt>` + `X-Session-Id`
    pair (fallback). The corpus is scope-INDEPENDENT; credentials authenticate the fetch
    only, never entering a node or a message (D5). Returns a COMBINED export dict.
  * `FixtureCorpusClient` — reads the frozen offline seed YAML
    (`tests/fixtures/corpus/{blueprints,knowledge}.yaml`) instead of HTTP. Those fixtures
    do NOT carry `source`/`verified`; the client presents them as canon and the loader/seed
    DEFAULTS (`source="mcp"`, `verified=True`) do the rest.

NOTE: the old per-turn `CorpusCache` one-shot seed trigger was REMOVED in the
singleton-hydrator redesign — the runtime no longer seeds on the request path; the
hydrator daemon owns the seed loop, building an `HttpCorpusClient` directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

_logger = logging.getLogger(__name__)


class CorpusClientError(Exception):
    """A corpus export fetch was rejected or returned an unusable body.

    Carries the endpoint's stable *code* (when the JSON error body supplies one) so the
    cache can log it; the cache degrades on ANY error, so this is never surfaced to the
    model/client.
    """

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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


def _headers(jwt: str, session_id: str) -> dict[str, str]:
    # The SAME header pair the read plane + catalog/scratch side-channels send (D5): the
    # JWT authenticates WHO; X-Session-Id carries the session binding. Neither is ever
    # reflected into a corpus node or a model-visible message.
    return {"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}


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
    """Real `CorpusClient` over the live MCP corpus-export routes (Layer 2+).

    Two auth modes (mirroring `HttpCatalogClient`):
      * per-request JWT (default) — `Authorization: Bearer <jwt>` + `X-Session-Id`.
      * static SERVICE KEY (`service_key=`) — `X-Service-Key: <key>` INSTEAD of the
        Bearer/session pair, so the singleton hydrator daemon authenticates the corpus
        exports with a static key and no user JWT. When set, the per-request
        `jwt`/`session_id` args are IGNORED.
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
        """The auth headers for one fetch — the static service key when configured,
        else the per-request Bearer/session pair. Branches on `self._service_key`."""
        if self._service_key:
            return {"X-Service-Key": self._service_key}
        return _headers(jwt, session_id)

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

    Credentials are accepted (same interface) but IGNORED: the fixtures are not
    scope-sensitive. The fixtures carry NO `source`/`verified` (and no per-corpus sha);
    the loader/seed defaults present them as trusted canon. This is the client the suite
    and fully-offline runs use.
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


def _error_from_response(resp: httpx.Response, path: str) -> CorpusClientError:
    code: str | None = None
    message = f"corpus export {path} returned HTTP {resp.status_code}"
    try:
        body = resp.json()
        if isinstance(body, dict):
            code = body.get("code")
            message = body.get("error") or message
    except ValueError:
        pass
    return CorpusClientError(code, message)


__all__ = [
    "CorpusClient",
    "CorpusClientError",
    "FixtureCorpusClient",
    "HttpCorpusClient",
]
