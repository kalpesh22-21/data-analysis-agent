"""CatalogClient + CatalogCache — the runtime side of the MCP `/catalog/export` (D75).

The export is scope-INDEPENDENT: the first successful fetch freezes both immutable
handles process-wide, and credentials authenticate that one fetch only — they never
enter a handle or a message (D5). Fail-closed: with nothing cached yet, a failed fetch
returns EMPTY handles that are NOT cached, degrading provenance to `None`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from data_agent.runtime.mcp._transport import (
    SideChannelError,
    auth_headers,
    error_from_response,
)
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


class CatalogClientError(SideChannelError):
    """A `/catalog/export` fetch was rejected or returned an unusable body.

        Carries the endpoint's stable *code* when the JSON error body supplies one. Never
        surfaced to the model or client — the cache degrades fail-closed on any error.
    """


class CatalogClient(Protocol):
    """The transport seam `CatalogCache` depends on (HTTP + fixture share it)."""

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
        """Return the parsed `{"catalog_sha": ..., "catalog": {...}}` export dict.

                Raises `CatalogClientError` on a transport/parse failure so the cache can
                degrade fail-closed.
        """
        ...


class HttpCatalogClient:
    """Real `CatalogClient` over the live MCP `/catalog/export` route.

        Two auth modes: the per-request `Authorization: Bearer` + `X-Session-Id` pair
        (default), or a static `X-Service-Key` when `service_key=` is set — in which case
        the per-request `jwt`/`session_id` args are IGNORED and never reach the wire.
    """

    def __init__(
        self, base_url: str, *, timeout: float = 30.0, service_key: str | None = None
    ) -> None:
        # base_url is the …/catalog base (no trailing slash); `/export` is appended.
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._service_key = service_key or None

    def _auth_headers(self, *, jwt: str, session_id: str) -> dict[str, str]:
        """The auth headers for one fetch — the static service key when configured, else
                the per-request Bearer/session pair.
        """
        return auth_headers(service_key=self._service_key, jwt=jwt, session_id=session_id)

    async def fetch_export(self, *, jwt: str = "", session_id: str = "") -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}/export",
                    headers=self._auth_headers(jwt=jwt, session_id=session_id),
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

        Credentials are accepted for interface parity but IGNORED.
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


def _error_from_response(resp: httpx.Response) -> SideChannelError:
    return error_from_response(
        resp, error_class=CatalogClientError, description="catalog export endpoint"
    )


class CatalogCache:
    """Process-wide cache of the two catalog handles built from one MCP export.

        The first successfully-built pair serves every later turn/session/scope until
        `force_reload=True`, and both come from ONE export so they cannot disagree. A
        double-checked `asyncio.Lock` guards the cold fetch, so a slower racer can never
        replace an already-frozen handle. Fail-closed on any error (see module docstring).
    """

    def __init__(
        self,
        client: CatalogClient,
        *,
        on_catalog_loaded: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._catalog_handle: CatalogHandle | None = None
        self._semantic_handle: SemanticCatalogHandle | None = None
        self._lock = asyncio.Lock()
        # Optional one-shot side effect on the FIRST successful COLD fetch (B1
        # self-healing graph seed). Neo4j-AGNOSTIC: the cache only knows a
        # `dict -> Awaitable[None]` callback; app.py binds it to `load_catalog_graph`
        # against the retrieval driver (or `None` when Neo4j is absent).
        self._on_catalog_loaded = on_catalog_loaded
        self._graph_seed_done = False

    async def _ensure(self, *, jwt: str, session_id: str, force_reload: bool) -> None:
        # Already warm and not force-reloading — one fetch serves all turns. Cheap
        # lock-free fast path for the common (warm) case.
        if self._catalog_handle is not None and not force_reload:
            return
        # Captured INSIDE the lock (one-shot) but invoked AFTER the lock is released,
        # so the graph-seed callback never serializes concurrent first-turn waiters
        # behind it (the handle they need is already built + frozen). `None` unless
        # THIS call is the cold fetch that fires the one-shot seed.
        export_for_seed: dict[str, Any] | None = None
        async with self._lock:
            # Double-checked: a racer may have warmed the cache while we waited for
            # the lock — reuse its frozen handle instead of issuing a second fetch.
            if self._catalog_handle is not None and not force_reload:
                return
            # A forced reload re-arms the one-shot graph seed (M2): a `force_reload`
            # deliberately re-fetches, so the seed should run again against the fresh
            # export rather than being permanently disarmed by the first cold fetch.
            if force_reload:
                self._graph_seed_done = False
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
            # Arm the one-shot graph seed for AFTER the lock releases (the handle is
            # already built + frozen above, so this side effect blocks no reader).
            if self._on_catalog_loaded is not None and not self._graph_seed_done:
                self._graph_seed_done = True
                export_for_seed = export
        # Lock released. Fire the graph-seed callback at most once, OFF the lock, and
        # DEGRADE-not-fail: a graph-seed failure must never fail the catalog load or
        # the turn — the `CatalogHandle` the turn needs is already served regardless.
        if export_for_seed is not None and self._on_catalog_loaded is not None:
            try:
                await self._on_catalog_loaded(export_for_seed)
            except Exception:
                # Degrade-not-fail: the seed failure must never fail the turn (the
                # handle is already served). RE-ARM the one-shot (M2) so a transient
                # neo4j blip is retried on the NEXT cold fetch rather than leaving the
                # process unseeded for life. Safe under concurrency: a bool assignment,
                # and the next cold fetch re-checks the flag inside the lock.
                self._graph_seed_done = False
                _logger.exception(
                    "catalog graph seed callback failed; catalog load + turn unaffected "
                    "(re-armed for retry on the next cold fetch)"
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


def build_catalog_cache(
    settings: RuntimeSettings,
    *,
    on_catalog_loaded: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> CatalogCache:
    """Build a `CatalogCache` from settings — HTTP against the MCP, or the fixture.

        `catalog_source="fixture"` reads `catalog_fixture_file()`; anything else fetches
        `/catalog/export` from `catalog_api_url` (falling back to `mcp_url`).
        *on_catalog_loaded* is a one-shot callback invoked with the raw export dict on the
        first successful cold fetch; `None` (and no Neo4j) ⇒ no-op. A configured
        `mcp_service_key` makes the fetch ride the static key, so no per-request
        credential reaches the MCP export.
    """
    client: CatalogClient
    if settings.catalog_source == "fixture":
        client = FixtureCatalogClient(settings.catalog_fixture_file())
    else:
        client = HttpCatalogClient(
            settings.catalog_api_base(), service_key=settings.mcp_service_key or None
        )
    return CatalogCache(client, on_catalog_loaded=on_catalog_loaded)


__all__ = [
    "CatalogCache",
    "CatalogClient",
    "CatalogClientError",
    "FixtureCatalogClient",
    "HttpCatalogClient",
    "build_catalog_cache",
]
