"""Hydrator — the singleton neo4j seeder daemon (replaces the runtime's lazy per-turn
graph seed).

The runtime pods are pure READERS of the neo4j retrieval graph. This INDEPENDENT
`replicas:1` daemon (scripts/run_hydrator.py) owns the write path:

  * it authenticates to the MCP export routes with a STATIC service key (no user JWT —
    the old per-turn seed borrowed the FIRST turn's JWT, and made every pod sync);
  * it seeds neo4j on boot and re-seeds live on every poll cycle (default 60s);
  * it owns the DESTRUCTIVE nuke + rebuild on a dimension change (a changed embedding
    model), so a runtime pod never has to.

`run_once` is idempotent + cheap on an unchanged poll: the loaders' B1 sha-no-op fast
paths (`:CatalogMeta`/`:CorpusMeta` singletons) short-circuit BEFORE any embed/write, so
a steady-state poll is a couple of cheap reads. BUT the sha alone does NOT compare the
embedding MODEL/DIMENSION, so the hydrator explicitly probes the `source='mcp'` model +
the live vector-index dimension every cycle and routes a change correctly:

  * dimension change → `rebuild_mcp_corpus_partition(dimension=new)` (drop+recreate the
    vector indexes at the new dim, clear the mcp corpus + freshness singletons) then reseed;
  * same-dim model swap → clear the mcp corpus (so the mcp-scoped write-time parity guard
    can't trip) then reseed — re-embedding every mcp node at the new model;
  * otherwise → the normal sha-gated no-op path.

CRUCIAL data-safety: the hydrator uses the SCOPED `rebuild_mcp_corpus_partition`, NOT
`nuke_graph` — the scoped rebuild deletes ONLY the trusted `source='mcp'` corpus + the
freshness singletons and PRESERVES the `source='learning'` staging tier (human-promoted
content that is NOT in the MCP export and would never be re-seeded) and the `:Table`/
`:Column` catalog graph.

Single-runner: because this is a true `replicas:1` singleton, there is NO distributed
`claim_rebuild_lock` — a single writer needs no single-flight guard.

Degrade-not-fail: `run_forever` swallows any per-cycle exception (logs, retries next
interval), exactly like `learning/sweeper.py`. Neo4j-absent / embedding-absent — OR an
empty `MCP_SERVICE_KEY` (which would 401 every poll) — ⇒ `build_hydrator` returns `None`
and the entrypoint idles without crashing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from data_agent.runtime.catalog.export_client import HttpCatalogClient
from data_agent.runtime.config import hydrator_enabled
from data_agent.runtime.model.embedding_client import HttpEmbeddingClient
from data_agent.runtime.retrieval.corpus_client import HttpCorpusClient
from data_agent.runtime.retrieval.corpus_loader import (
    DimensionMismatchError,
    _fetch_existing_models,
    _fetch_existing_vector_dims,
    apply_schema,
    corpus_seeds_from_export,
    effective_corpus_sha,
    load_catalog_graph,
    load_corpus,
    rebuild_mcp_corpus_partition,
    resolve_embedding_dimension,
)
from data_agent.runtime.retrieval.vector_index import Neo4jVectorIndex

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from neo4j import AsyncDriver

    from data_agent.runtime.config import RuntimeSettings
    from data_agent.runtime.model.embedding_client import EmbeddingClient

_logger = logging.getLogger(__name__)


class Hydrator:
    """Seed the neo4j retrieval graph from the MCP exports, idempotently, on a poll loop.

    Holds the write-side infra: an async neo4j driver, an embedding client, and the two
    service-key export clients. `run_once` fetches the catalog + corpus exports, resolves
    the embedding dimension, and seeds via `load_catalog_graph`/`load_corpus` (gc=True —
    the hydrator is the single authoritative reconciler). A `DimensionMismatchError`
    triggers a nuke + full re-seed at the new dimension.
    """

    def __init__(
        self,
        *,
        driver: AsyncDriver,
        database: str,
        embedding_client: EmbeddingClient,
        catalog_client: Any,
        corpus_client: Any,
        model_id: str,
        configured_dimension: int | None,
        poll_interval_seconds: int,
    ) -> None:
        self._driver = driver
        self._database = database
        self._embedder = embedding_client
        self._catalog_client = catalog_client
        self._corpus_client = corpus_client
        self._model_id = model_id
        self._configured_dimension = configured_dimension
        self._poll_interval_seconds = poll_interval_seconds

    async def run_once(self) -> bool:
        """One poll cycle: fetch the exports and seed neo4j idempotently.

        Returns True iff a seed pass ran (or was a B1 no-op), False iff the kill-switch
        disabled the hydrator this cycle. Reads the kill-switch FRESH (uncached) FIRST —
        a disabled hydrator does no fetch, no embed, no write."""
        if not hydrator_enabled():
            _logger.info("hydrator disabled (HYDRATOR_ENABLED) — skipping this cycle")
            return False

        # Service-key clients: the jwt/session args are ignored when a key is configured.
        catalog_export = await self._catalog_client.fetch_export()
        corpus_export = await self._corpus_client.fetch_export()

        # Resolve the target dimension once per cycle. When unset, `resolve_embedding_
        # dimension` probes the live embedder (a single cheap embed); a configured value
        # is cross-checked against the model on the seed path (load_corpus S1).
        dimension = await resolve_embedding_dimension(
            self._embedder, configured=self._configured_dimension
        )

        # Detect a required reseed BEFORE the loaders' sha-gated fast path — the sha does
        # NOT compare the embedding model/dimension, so a same-content model/dim swap would
        # otherwise be silently skipped (→ recall's `WHERE embedding_model=<new>` returns
        # empty while /ready still reads 200: a silent fleet-wide recall loss).
        existing_models, existing_dims = await self._read_mcp_state()
        dim_changed = bool(existing_dims) and dimension not in existing_dims
        model_changed = bool(existing_models) and any(
            model != self._model_id for model in existing_models
        )

        try:
            if dim_changed:
                _logger.warning(
                    "EMBEDDING DIMENSION CHANGED (existing=%s → target=%d) — rebuilding the "
                    "source='mcp' partition at the new dimension (learning tier + catalog "
                    "graph PRESERVED).",
                    sorted(existing_dims),
                    dimension,
                )
                await rebuild_mcp_corpus_partition(
                    self._driver, dimension=dimension, database=self._database
                )
                await self._seed(catalog_export, corpus_export, dimension)
            elif model_changed:
                _logger.warning(
                    "EMBEDDING MODEL CHANGED (existing=%s → target=%r) at the same dimension "
                    "— clearing + re-embedding the source='mcp' partition (learning tier "
                    "PRESERVED).",
                    sorted(existing_models),
                    self._model_id,
                )
                await rebuild_mcp_corpus_partition(
                    self._driver, dimension=None, database=self._database
                )
                await self._seed(catalog_export, corpus_export, dimension)
            else:
                await self._seed(catalog_export, corpus_export, dimension)
        except DimensionMismatchError:
            # Safety net: a stale-index dimension error the pre-checks somehow missed (e.g.
            # a partially-provisioned graph) must NOT wedge `run_forever` in a retry loop —
            # route it to the SCOPED rebuild (learning tier preserved) + reseed. A model
            # change never reaches here: it is handled proactively above (which clears the
            # mcp nodes so load_corpus's parity guard can't raise). A NON-dimension
            # CorpusLoadError (a malformed blueprint — an authoring error a rebuild can't
            # fix) is deliberately NOT caught: it propagates to `run_forever`, which logs +
            # retries WITHOUT a destructive rebuild.
            _logger.warning(
                "hydrator seed hit a DimensionMismatchError the pre-check missed — routing "
                "to a scoped source='mcp' rebuild at dimension=%d.",
                dimension,
            )
            await rebuild_mcp_corpus_partition(
                self._driver, dimension=dimension, database=self._database
            )
            await self._seed(catalog_export, corpus_export, dimension)
        return True

    async def _read_mcp_state(self) -> tuple[set[str], set[int]]:
        """The current `source='mcp'` embedding model(s) + the live vector-index
        dimension(s) — the inputs to the model/dimension-change detection. Both reads are
        scoped/introspective (no writes). An empty graph yields two empty sets (→ the
        normal seed path, which creates the index at the target dimension)."""
        async with self._driver.session(database=self._database) as session:
            models = await _fetch_existing_models(session)
            dims = await _fetch_existing_vector_dims(session)
        return models, dims

    async def _seed(
        self, catalog_export: dict[str, Any], corpus_export: dict[str, Any], dimension: int
    ) -> None:
        """Idempotent seed: schema → catalog graph → corpus, all at *dimension*, gc=True.

        The loaders' B1 sha-no-op fast paths make an unchanged poll a cheap no-op (they
        short-circuit BEFORE embedding/writing). `apply_schema` raises
        `DimensionMismatchError` on a stale-dimension index; `load_corpus` can too —
        both propagate to `run_once`'s nuke + rebuild handler."""
        await apply_schema(self._driver, dimension=dimension, database=self._database)
        await load_catalog_graph(
            self._driver, catalog_export, database=self._database, gc=True
        )
        blueprints, knowledge = corpus_seeds_from_export(corpus_export)
        report = await load_corpus(
            self._driver,
            self._embedder,
            blueprints,
            knowledge,
            model_id=self._model_id,
            database=self._database,
            corpus_sha=effective_corpus_sha(corpus_export),
            gc=True,
            dimension=dimension,
        )
        if report.skipped:
            _logger.info("hydrator poll: corpus already current (B1 no-op)")
        else:
            _logger.info(
                "hydrator seeded: %d blueprint(s), %d knowledge chunk(s) at dimension=%d",
                report.blueprints_written,
                report.knowledge_written,
                dimension,
            )

    async def run_forever(self, *, sleep: Callable[[float], Awaitable[None]]) -> None:
        """Periodic poll loop (the entrypoint). *sleep* is injected (`asyncio.sleep`) so
        it is unit-drivable. Mirrors `learning/sweeper.py::run_forever`: a transient
        fetch/seed error must not kill the daemon — log it and retry next interval."""
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - a transient fetch/seed error must not kill
                # the daemon; log and retry next cycle (mirrors the learning sweeper).
                _logger.exception("hydrator poll cycle failed; retrying next interval")
            await sleep(self._poll_interval_seconds)

    async def close(self) -> None:
        """Close the neo4j driver pool (entrypoint shutdown)."""
        await self._driver.close()


def build_hydrator(
    settings: RuntimeSettings, *, tracer: Any = None
) -> Hydrator | None:
    """Build a `Hydrator` from settings — or `None` when the prerequisites are absent.

    Requires BOTH a neo4j URL AND a configured embedding API (the seeder must embed the
    corpus and write to a graph). Absent either, returns `None` so the entrypoint idles
    without crashing — the same Phase-0-parity gate `app.py` applies to the vector index.

    Reuses `Neo4jVectorIndex` to obtain a driver bound to the SAME default database
    (`"neo4j"`) the runtime reads from, so the hydrator seeds exactly what recall serves.
    The two export clients are built with `service_key=settings.mcp_service_key` so the
    daemon authenticates the MCP exports with the static key, never a user JWT.

    An EMPTY `mcp_service_key` also returns `None`: without it the export clients fall back
    to per-request-JWT mode and the daemon (which has no user JWT) would send BLANK Bearer
    creds → 401 every poll → neo4j never seeds → every runtime pod fails its /ready gate
    FOREVER. Refuse to poll with garbage creds; log an unmistakable error and idle."""
    if not settings.neo4j_url or not settings.embedding_api_url:
        return None
    if not settings.mcp_service_key:
        _logger.error(
            "HYDRATOR MISCONFIGURED: neo4j + the embedding API are configured but "
            "MCP_SERVICE_KEY is EMPTY. The hydrator authenticates the MCP export routes "
            "with a STATIC service key (it has no user JWT); with an empty key every poll "
            "would send blank credentials and 401, so neo4j would NEVER seed and every "
            "runtime pod would fail its /ready gate forever. Refusing to start — set "
            "MCP_SERVICE_KEY, or disable the hydrator (HYDRATOR_ENABLED / "
            "components.hydrator.enabled)."
        )
        return None

    embedding_client = HttpEmbeddingClient(
        url=settings.embedding_api_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        timeout_seconds=settings.embedding_timeout_seconds,
        tracer=tracer,
    )
    # Reuse the vector-index construction so the driver + database match the reader's.
    index = Neo4jVectorIndex(
        url=settings.neo4j_url,
        auth=(settings.neo4j_username, settings.neo4j_password),
        expected_model=settings.embedding_model,
        timeout_seconds=settings.neo4j_timeout_seconds,
        tracer=tracer,
    )
    service_key = settings.mcp_service_key or None
    catalog_client = HttpCatalogClient(settings.catalog_api_base(), service_key=service_key)
    corpus_client = HttpCorpusClient(settings.corpus_api_base(), service_key=service_key)
    return Hydrator(
        driver=index.driver,
        database=index.database,
        embedding_client=embedding_client,
        catalog_client=catalog_client,
        corpus_client=corpus_client,
        model_id=settings.embedding_model,
        configured_dimension=settings.embedding_dimension,
        poll_interval_seconds=settings.hydrator_poll_interval_seconds,
    )


__all__ = ["Hydrator", "build_hydrator"]
