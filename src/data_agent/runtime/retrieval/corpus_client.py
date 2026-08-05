"""CorpusClient + CorpusCache — the runtime side of the MCP corpus exports (governed
corpus, Phase 2).

The MCP is the single source of TRUSTED recall canon. Two auth-required routes serve
the git-versioned corpus:

  - `GET /blueprints/export` → `{"blueprints_sha": <sha>, "blueprints": {<id>: <entry>}}`
  - `GET /knowledge/export`  → `{"knowledge_sha": <sha>, "knowledge": {<id>: <entry>}}`

Each entry is the verbatim blueprint/knowledge fields PLUS `source="mcp"` +
`verified=True` injected at export time. The runtime projects these into the neo4j
recall corpus as the `source="mcp"` partition — the ONLY partition the agent recall
serves (the trust gate in `vector_index`). A separate learning-staging tier
(`source="learning"`) recall ignores.

This module mirrors `catalog/export_client.py` EXACTLY (transport + process-wide cache):

  - `CorpusClient` (Protocol) with two implementations:
      * `HttpCorpusClient`    — `GET {root}/blueprints/export` + `GET {root}/knowledge/export`
        on the SAME MCP host, behind the SAME `JWTAuthMiddleware` as the read plane, so
        it rides the SAME credential binding (`Authorization: Bearer <jwt>` +
        `X-Session-Id: <session_id>`). The corpus is scope-INDEPENDENT (every principal
        sees the same canon); the JWT authenticates the fetch only, never entering a
        node or a message (D5). Returns a COMBINED export dict.
      * `FixtureCorpusClient` — reads the frozen offline seed YAML
        (`tests/fixtures/corpus/{blueprints,knowledge}.yaml`) instead of HTTP. Those
        fixtures do NOT carry `source`/`verified`; the client presents them as canon and
        the loader/seed DEFAULTS (`source="mcp"`, `verified=True`) do the rest.

  - `CorpusCache` — mirrors `CatalogCache`'s cold-fetch-and-seed shape. The export is
    scope-INDEPENDENT, so the FIRST successful fetch (with whichever turn's credentials
    triggers it) fires the one-shot `on_corpus_loaded` seed callback PROCESS-WIDE, then
    every subsequent turn is a warm no-op. The seed callback is fired OFF the lock, at
    most once, DEGRADE-not-fail with re-arm on failure (copied from `CatalogCache`).

Fail-closed / degrade-not-fail: a fetch failure with nothing warm is swallowed
(logged server-side ONLY) and NOT cached — the next turn retries. A seed-callback
failure never fails the turn (the callback re-arms so a transient neo4j blip retries).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx
import yaml

if TYPE_CHECKING:
    from data_agent.runtime.config import RuntimeSettings

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
    """The transport seam `CorpusCache` depends on (HTTP + fixture share it)."""

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
    """Real `CorpusClient` over the live MCP corpus-export routes (Layer 2+)."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        # base_url is the MCP HOST ROOT (no trailing slash); the two route paths are
        # appended. The routes live at the host root, NOT under `/mcp` or `/catalog`.
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _get(
        self, path: str, *, jwt: str, session_id: str, expected_key: str
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}{path}", headers=_headers(jwt, session_id)
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

    async def fetch_export(self, *, jwt: str, session_id: str) -> dict[str, Any]:
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


class CorpusCache:
    """Process-wide one-shot corpus seed trigger built on the MCP exports.

    Unlike `CatalogCache` (which builds handles a turn consumes), the corpus is consumed
    by RECALL reading neo4j directly — this cache's sole job is to fire the one-shot
    `on_corpus_loaded` seed callback (which projects the export into the neo4j
    `source='mcp'` partition via `load_corpus`) EXACTLY ONCE on the first successful
    fetch, then serve warm no-ops.

    Mirrors `CatalogCache`: lazily fetch on first use with the CURRENT turn's
    `jwt`/`session_id`; the cold fetch is guarded by an `asyncio.Lock` (double-checked)
    so concurrent first-turns issue AT MOST one in-flight fetch. The seed callback is
    captured under the lock but AWAITED after it is released, so a slow seed never
    serializes concurrent waiters. Degrade-not-fail: a fetch failure is swallowed +
    uncached (next turn retries); a seed-callback failure re-arms the one-shot.
    """

    def __init__(
        self,
        client: CorpusClient,
        *,
        on_corpus_loaded: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._loaded = False
        self._lock = asyncio.Lock()
        # The one-shot corpus seed side effect on the FIRST successful COLD fetch.
        # Neo4j-AGNOSTIC: the cache only knows a `dict -> Awaitable[None]` callback;
        # app.py binds it to `load_corpus` against the retrieval driver (or `None` when
        # Neo4j is absent, so the whole feature is a byte-identical no-op).
        self._on_corpus_loaded = on_corpus_loaded
        self._seed_done = False

    @property
    def is_loaded(self) -> bool:
        """True once a cold fetch succeeded (the seed callback has been fired at least
        once). Lets the composition root avoid re-scheduling the warm trigger."""
        return self._loaded

    async def ensure_seeded(
        self, *, jwt: str, session_id: str, force_reload: bool = False
    ) -> bool:
        """Fetch the corpus export once and fire the one-shot seed callback. Returns
        True iff the corpus is now loaded (a cold success or an already-warm cache);
        False iff the cold fetch failed and nothing is cached (the caller may retry)."""
        # Warm fast path — one fetch/seed serves every turn. Lock-free common case.
        if self._loaded and not force_reload:
            return True
        export_for_seed: dict[str, Any] | None = None
        async with self._lock:
            if self._loaded and not force_reload:
                return True
            if force_reload:
                self._seed_done = False
            try:
                export = await self._client.fetch_export(jwt=jwt, session_id=session_id)
            except Exception:
                # Degrade-not-fail: never crash the turn. Log server-side ONLY. Leave
                # the cache cold so the NEXT turn retries the fetch.
                _logger.exception(
                    "corpus export fetch failed; recall corpus seed deferred to a later turn"
                )
                return False
            self._loaded = True
            # Drift observability: surface the two shas + counts on the cold-fetch
            # success path. Shape-only — the shas are content digests, the counts are
            # cardinalities (no PII/credentials).
            blueprints = export.get("blueprints")
            knowledge = export.get("knowledge")
            _logger.info(
                "corpus cache warmed from export: blueprints_sha=%s knowledge_sha=%s "
                "blueprints=%d knowledge=%d",
                export.get("blueprints_sha"),
                export.get("knowledge_sha"),
                len(blueprints) if isinstance(blueprints, dict) else 0,
                len(knowledge) if isinstance(knowledge, dict) else 0,
            )
            # Arm the one-shot seed for AFTER the lock releases.
            if self._on_corpus_loaded is not None and not self._seed_done:
                self._seed_done = True
                export_for_seed = export
        # Lock released. Fire the seed callback at most once, OFF the lock, and
        # DEGRADE-not-fail: a seed failure must never fail the turn (recall degrades to
        # an empty/last-good corpus regardless). RE-ARM the one-shot so a transient
        # neo4j blip is retried on the NEXT cold fetch.
        if export_for_seed is not None and self._on_corpus_loaded is not None:
            try:
                await self._on_corpus_loaded(export_for_seed)
            except Exception:
                self._seed_done = False
                self._loaded = False  # allow a later turn to re-fetch + retry the seed
                _logger.exception(
                    "corpus seed callback failed; turn unaffected "
                    "(re-armed for retry on the next turn)"
                )
                return False
        return True


def build_corpus_cache(
    settings: RuntimeSettings,
    *,
    on_corpus_loaded: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> CorpusCache:
    """Build a `CorpusCache` from settings — HTTP against the MCP, or the fixtures.

    `corpus_source="fixture"` (offline + tests) reads the two seed YAML files;
    anything else (default `"mcp"`) fetches from the MCP corpus routes derived from
    `mcp_url` (or `corpus_api_url` when set).

    *on_corpus_loaded* (the one-shot recall-corpus seed): an optional callback the cache
    invokes with the raw combined export dict on the first successful cold fetch. `None`
    (default, and when Neo4j is absent) ⇒ a byte-identical no-op."""
    client: CorpusClient
    if settings.corpus_source == "fixture":
        client = FixtureCorpusClient(
            settings.corpus_blueprints_fixture_file(),
            settings.corpus_knowledge_fixture_file(),
        )
    else:
        client = HttpCorpusClient(settings.corpus_api_base())
    return CorpusCache(client, on_corpus_loaded=on_corpus_loaded)


__all__ = [
    "CorpusCache",
    "CorpusClient",
    "CorpusClientError",
    "FixtureCorpusClient",
    "HttpCorpusClient",
    "build_corpus_cache",
]
