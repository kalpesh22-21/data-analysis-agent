"""CatalogClient + CatalogCache — the runtime side of the MCP `/catalog/export` (D75 Wave 1b).

The runtime no longer ships its own copy of `databaseSchemaDocs/`. It rebuilds the
immutable `CatalogHandle` / `SemanticCatalogHandle` from the MCP's semantic-catalog
export instead — the MCP is the single source of truth (adopt-and-extend, D75).

Two pieces, mirroring `mcp/client.py` (transport) + `mcp/tool_schema.py` (cache):

  - `CatalogClient` (Protocol) with two implementations:
      * `HttpCatalogClient`    — `GET {base}/export` on the SAME MCP host, behind the
        SAME `JWTAuthMiddleware` as the read plane, so it rides the SAME credential
        binding (`Authorization: Bearer <jwt>` + `X-Session-Id: <session_id>`) — the
        catalog export needs a valid JWT even though the catalogue itself is
        scope-INDEPENDENT (every principal sees the same catalog; only per-tool
        RESULTS are scope-filtered by the MCP). This is the SAME side-channel pattern
        `mcp/scratch_client.py` uses for the D93 scratch routes.
      * `FixtureCatalogClient` — reads the frozen `tests/fixtures/catalog_export.json`
        (or any configured path) instead of HTTP. This is how OFFLINE runs and the
        test suite obtain the export after `databaseSchemaDocs/` is deleted.

  - `CatalogCache` — mirrors `ToolSchemaCache` EXACTLY: the export is scope-INDEPENDENT,
    so the FIRST successful fetch (made with whichever turn's credentials happens to
    trigger it) builds + freezes the two immutable handles PROCESS-WIDE and serves every
    subsequent turn/session/scope until an explicit `force_reload`. Credentials are used
    ONLY to authenticate that one fetch — they never enter a handle or a message (D5).

Fail-closed semantics (documented per brief): if the fetch (or handle build) fails
and NOTHING is cached yet, `get_catalog_handle` returns an EMPTY `CatalogHandle`
(and `get_semantic_catalog_handle` an empty `SemanticCatalogHandle`) WITHOUT caching
it — the turn does NOT crash, the real error is logged server-side only, and the
next turn retries the fetch. An empty catalog degrades provenance fail-closed:
`capture_provenance` cannot resolve any column against an empty schema, so it returns
`None` (undetermined) → the trail entry is dropped from replay by the D44 scope filter
— the existing absent-catalog behavior. If a PRIOR fetch already succeeded, a later
failure is swallowed and the still-warm cached handle keeps serving.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from data_agent.runtime.provenance.catalog_handle import (
    CatalogHandle,
    SemanticCatalogHandle,
    load_catalog_handles_from_export,
)

if TYPE_CHECKING:
    from data_agent.runtime.config import RuntimeSettings

_logger = logging.getLogger(__name__)

# The fail-closed handles served when no export has ever been fetched. Empty ⇒
# provenance capture resolves no columns ⇒ `None` (undetermined) ⇒ dropped from
# replay (D44). Shared singletons: both are deeply immutable, so sharing is safe.
_EMPTY_CATALOG_HANDLE = CatalogHandle({})
_EMPTY_SEMANTIC_HANDLE = SemanticCatalogHandle({})


class CatalogClientError(Exception):
    """A `/catalog/export` fetch was rejected or returned an unusable body.

    Carries the endpoint's stable *code* (when the JSON error body supplies one)
    so the cache can log it; the cache degrades fail-closed on ANY error, so this
    is never surfaced to the model/client.
    """

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CatalogClient(Protocol):
    """The transport seam `CatalogCache` depends on (HTTP + fixture share it)."""

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        """Return the parsed `{"catalog_sha": ..., "catalog": {...}}` export dict.

        Raises `CatalogClientError` on a transport/parse failure so the cache can
        degrade fail-closed.
        """
        ...


def _headers(jwt: str, session_id: str) -> dict[str, str]:
    # The SAME header pair the read plane + scratch side-channel send (D5): the JWT
    # authenticates WHO; X-Session-Id carries the session binding. Neither is ever
    # reflected into a handle or a model-visible message.
    return {"Authorization": f"Bearer {jwt}", "X-Session-Id": session_id}


class HttpCatalogClient:
    """Real `CatalogClient` over the live MCP `/catalog/export` route (Layer 2+)."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        # base_url is the …/catalog base (no trailing slash); `/export` is appended.
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}/export",
                    headers=_headers(jwt, session_id),
                )
        except httpx.HTTPError as exc:
            raise CatalogClientError(None, f"catalog export request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        try:
            body = resp.json()
        except ValueError as exc:
            raise CatalogClientError(None, "catalog export returned a non-JSON body") from exc
        if not isinstance(body, dict) or "catalog" not in body:
            raise CatalogClientError(None, "catalog export body missing a 'catalog' object")
        return body


class FixtureCatalogClient:
    """Offline/test `CatalogClient` — reads a frozen export JSON from disk.

    Credentials are accepted (same interface) but IGNORED: the fixture is not
    scope-sensitive. This is the client the suite and offline runs use once
    `databaseSchemaDocs/` is gone.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        try:
            with self._path.open(encoding="utf-8") as fh:
                body = json.load(fh)
        except (OSError, ValueError) as exc:
            raise CatalogClientError(
                None, f"catalog fixture at {self._path} could not be read: {exc}"
            ) from exc
        if not isinstance(body, dict) or "catalog" not in body:
            raise CatalogClientError(
                None, f"catalog fixture at {self._path} missing a 'catalog' object"
            )
        return body


def _error_from_response(resp: httpx.Response) -> CatalogClientError:
    code: str | None = None
    message = f"catalog export endpoint returned HTTP {resp.status_code}"
    try:
        body = resp.json()
        if isinstance(body, dict):
            code = body.get("code")
            message = body.get("error") or message
    except ValueError:
        pass
    return CatalogClientError(code, message)


class CatalogCache:
    """Process-wide cache of the two catalog handles built from the MCP export.

    Mirrors `ToolSchemaCache`: lazily fetch on first use with the CURRENT turn's
    `jwt`/`session_id` (the live MCP authenticates `/catalog/export` too), then serve
    the FIRST successfully-built handles for every subsequent turn/session/scope until
    `force_reload=True`. The two handles are built together from one export so they can
    never disagree. Fail-closed on any error (see module docstring).

    The cold-fetch-and-freeze is guarded by an `asyncio.Lock` (double-checked: the
    cache is re-read after the lock is acquired). Concurrent first-turns therefore
    issue AT MOST one in-flight fetch — the first successful build wins and every
    later racer reuses it, rather than a slower in-flight fetch REPLACING an
    already-frozen handle. This is stronger than `ToolSchemaCache`'s lock-free
    freeze, chosen deliberately because the catalog is security-adjacent (it drives
    provenance/scope resolution) and a mid-race MCP redeploy could otherwise swap a
    frozen handle for content-divergent one. `force_reload=True` still forces a
    re-fetch (it bypasses the warm-cache short-circuit inside the lock).
    """

    def __init__(self, client: CatalogClient) -> None:
        self._client = client
        self._catalog_handle: CatalogHandle | None = None
        self._semantic_handle: SemanticCatalogHandle | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self, *, jwt: str, session_id: str, force_reload: bool) -> None:
        # Already warm and not force-reloading — one fetch serves all turns. Cheap
        # lock-free fast path for the common (warm) case.
        if self._catalog_handle is not None and not force_reload:
            return
        async with self._lock:
            # Double-checked: a racer may have warmed the cache while we waited for
            # the lock — reuse its frozen handle instead of issuing a second fetch.
            if self._catalog_handle is not None and not force_reload:
                return
            try:
                export = await self._client.fetch_export(jwt=jwt, session_id=session_id)
                catalog_handle, semantic_handle = load_catalog_handles_from_export(export)
            except Exception:
                # Fail-closed: never crash the turn. Log server-side ONLY (never to the
                # model/client). Leave the cache untouched — if a prior fetch warmed it,
                # that handle keeps serving; otherwise the empty fallback is returned
                # (uncached) and the NEXT turn retries the fetch.
                _logger.exception(
                    "catalog export fetch/build failed; provenance will degrade fail-closed"
                )
                return
            self._catalog_handle = catalog_handle
            self._semantic_handle = semantic_handle
            # Drift observability: surface the export's catalog_sha + table count on
            # the cold-fetch success path so runtime-vs-consumer catalog drift is
            # visible in logs/traces. Shape-only — no PII/credentials (the sha is a
            # content digest; the count is a table cardinality).
            catalog_sha = export.get("catalog_sha")
            table_count = len(export["catalog"]) if isinstance(export.get("catalog"), dict) else 0
            _logger.info(
                "catalog cache warmed from export: catalog_sha=%s tables=%d",
                catalog_sha,
                table_count,
            )

    async def get_catalog_handle(
        self, *, jwt: str, session_id: str, force_reload: bool = False
    ) -> CatalogHandle:
        await self._ensure(jwt=jwt, session_id=session_id, force_reload=force_reload)
        return self._catalog_handle if self._catalog_handle is not None else _EMPTY_CATALOG_HANDLE

    async def get_semantic_catalog_handle(
        self, *, jwt: str, session_id: str, force_reload: bool = False
    ) -> SemanticCatalogHandle:
        await self._ensure(jwt=jwt, session_id=session_id, force_reload=force_reload)
        return (
            self._semantic_handle if self._semantic_handle is not None else _EMPTY_SEMANTIC_HANDLE
        )


def build_catalog_cache(settings: RuntimeSettings) -> CatalogCache:
    """Build a `CatalogCache` from settings — HTTP against the MCP, or the fixture.

    `catalog_source="fixture"` (offline + tests) reads `catalog_fixture_file()`;
    anything else (default `"mcp"`) fetches from the MCP `/catalog/export` route
    derived from `mcp_url` (or `catalog_api_url` when set)."""
    client: CatalogClient
    if settings.catalog_source == "fixture":
        client = FixtureCatalogClient(settings.catalog_fixture_file())
    else:
        client = HttpCatalogClient(settings.catalog_api_base())
    return CatalogCache(client)


__all__ = [
    "CatalogCache",
    "CatalogClient",
    "CatalogClientError",
    "FixtureCatalogClient",
    "HttpCatalogClient",
    "build_catalog_cache",
]
