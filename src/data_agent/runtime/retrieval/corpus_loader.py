"""corpus_loader — offline write path for the neo4j retrieval corpus (Slice 2).

`load_corpus(...)` is a reusable, idempotent bulk upsert of a small, curated,
TRUSTED seed (neo4j-corpus-design §3): it embeds every blueprint `intent` and
knowledge `text` through the REAL `HttpEmbeddingClient` (the same D71 endpoint
the online path uses — parity by construction), MERGE-by-id upserts the nodes
with the embedding + `embedding_model` stamp + the denormalized `uses` list
property, and writes the reserved `:Column`/`:Table` nodes + `:USES`/`:OF_TABLE`
edges in the SAME transaction (the D60 graph shape; unread at recall in Slice 2).

Parity is STRICT at write (§3.3): the loader refuses to write two different
embedding-model ids into one index — a mixed index is silently broken, so
write-time is the right place to fail loudly. Read-time parity is a DEGRADE
(`Neo4jVectorIndex` filters by `expected_model` → `[]` on mismatch, D86).

NOT Track B (§3.4): no `canonical_key`/dedup, no leakage gate, no promotion
lifecycle — the reserved lifecycle/provenance properties are seeded trivially
(`status=validated`, `created_by=seed`, `hit_count=0`) and simply not read by
the recall path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from neo4j import AsyncDriver, AsyncManagedTransaction

    from data_agent.runtime.model.embedding_client import EmbeddingClient

_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Seed value objects + fixtures
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BlueprintSeed:
    """One hand-authored blueprint fixture (neo4j-corpus-design §3.2).

    `uses` MUST be byte-exact `"database.table.column"` scope keys (the HR
    warehouse the ClickHouse seed + Semantic Catalog describe) or the read-path
    scope pre-filter silently drops the blueprint (§8 highest-risk contract).
    """

    id: str
    intent: str
    slots_summary: str
    uses: list[str]
    status: str = "validated"
    drift_status: str = "clean"
    catalog_sha: str = ""


@dataclass(frozen=True)
class KnowledgeSeed:
    """One hand-authored global-knowledge fixture (entity-agnostic)."""

    id: str
    text: str
    doc_id: str
    title: str | None = None
    status: str = "validated"


@dataclass(frozen=True)
class LoadReport:
    """Outcome of a `load_corpus` run — counts + the model the corpus was
    stamped with (for a CLI/exit summary)."""

    model_id: str
    blueprints_written: int
    knowledge_written: int
    columns_written: int
    tables_written: int


class CorpusLoadError(Exception):
    """Raised on a write-time parity violation (mixed embedding models, §3.3)."""


def load_seed_fixtures(
    corpus_dir: Path | str,
) -> tuple[list[BlueprintSeed], list[KnowledgeSeed]]:
    """Read `blueprints.yaml` + `knowledge.yaml` under *corpus_dir* into seeds.

    Raises `CorpusLoadError` on a duplicate id (within OR across the two files,
    QA flag 6): MERGE-by-id would silently let a copy-pasted id overwrite in
    place, masking an authoring mistake — fail loudly instead.
    """
    root = Path(corpus_dir)
    blueprints = [
        BlueprintSeed(**item) for item in _read_yaml_list(root / "blueprints.yaml")
    ]
    knowledge = [
        KnowledgeSeed(**item) for item in _read_yaml_list(root / "knowledge.yaml")
    ]
    _reject_duplicate_ids([b.id for b in blueprints] + [k.id for k in knowledge])
    return blueprints, knowledge


def _reject_duplicate_ids(ids: list[str]) -> None:
    seen: set[str] = set()
    dupes: set[str] = set()
    for id_ in ids:
        (dupes if id_ in seen else seen).add(id_)
    if dupes:
        raise CorpusLoadError(
            f"Duplicate seed id(s) in the corpus fixtures: {sorted(dupes)!r}."
        )


def _read_yaml_list(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or []
    if not isinstance(data, list):
        raise CorpusLoadError(f"Fixture {path} must be a YAML list.")
    return data


# --------------------------------------------------------------------------
# Schema DDL — idempotent constraints + native vector indexes (§1.4)
# --------------------------------------------------------------------------

# Every statement is idempotent (`IF NOT EXISTS`) so `apply_schema` is safe to
# re-run — the loader's whole write path (schema + upsert) is re-runnable.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE CONSTRAINT blueprint_id IF NOT EXISTS "
    "FOR (b:Blueprint) REQUIRE b.id IS UNIQUE",
    "CREATE CONSTRAINT knowledge_id IF NOT EXISTS "
    "FOR (k:KnowledgeChunk) REQUIRE k.id IS UNIQUE",
    "CREATE CONSTRAINT column_key IF NOT EXISTS "
    "FOR (c:Column) REQUIRE c.key IS UNIQUE",
    "CREATE CONSTRAINT table_key IF NOT EXISTS "
    "FOR (t:Table) REQUIRE t.key IS UNIQUE",
    "CREATE VECTOR INDEX blueprint_intent_vec IF NOT EXISTS "
    "FOR (b:Blueprint) ON (b.intent_embedding) "
    "OPTIONS { indexConfig: { `vector.dimensions`: 768, "
    "`vector.similarity_function`: 'cosine' } }",
    "CREATE VECTOR INDEX knowledge_text_vec IF NOT EXISTS "
    "FOR (k:KnowledgeChunk) ON (k.text_embedding) "
    "OPTIONS { indexConfig: { `vector.dimensions`: 768, "
    "`vector.similarity_function`: 'cosine' } }",
)


# --------------------------------------------------------------------------
# Upsert Cypher (§3.1) — MERGE-by-id so a re-run updates in place, never dupes
# --------------------------------------------------------------------------

_UPSERT_BLUEPRINT = """
MERGE (b:Blueprint {id: $id})
SET b.intent = $intent,
    b.slots_summary = $slots_summary,
    b.intent_embedding = $embedding,
    b.embedding_model = $model,
    b.uses = $uses,
    b.status = $status,
    b.drift_status = $drift_status,
    b.catalog_sha = $catalog_sha,
    b.created_by = 'seed',
    b.created_at = coalesce(b.created_at, datetime()),
    b.hit_count = coalesce(b.hit_count, 0)
"""

# Reserved graph shape (§1.3): the transitive USES closure written as edges,
# same-txn with the denormalized `uses` property so they cannot drift. Unread at
# recall in Slice 2. S1: DELETE this blueprint's existing :USES edges FIRST, so a
# re-seed with a SHRUNK uses set leaves no phantom edges (MERGE alone never
# removes stale edges). Runs unconditionally per blueprint (even when the new
# use-set is empty), so the DELETE always clears stale edges. Orphan :Column
# nodes left with no inbound :USES are NOT garbage-collected here (a shared
# concept may still be referenced by other blueprints, and stale leaf columns are
# harmless/unread in Slice 2) — a dedicated GC is deferred to the graph consumer.
_REWRITE_BLUEPRINT_EDGES = """
MATCH (b:Blueprint {id: $id})
OPTIONAL MATCH (b)-[r:USES]->()
DELETE r
WITH DISTINCT b
UNWIND $use_edges AS ue
MERGE (c:Column {key: ue.column_key})
MERGE (t:Table {key: ue.table_key})
MERGE (b)-[:USES]->(c)
MERGE (c)-[:OF_TABLE]->(t)
"""

_UPSERT_KNOWLEDGE = """
MERGE (k:KnowledgeChunk {id: $id})
SET k.title = $title,
    k.text = $text,
    k.text_embedding = $embedding,
    k.embedding_model = $model,
    k.doc_id = $doc_id,
    k.status = $status,
    k.created_by = 'seed',
    k.created_at = coalesce(k.created_at, datetime())
"""

_EXISTING_MODELS = """
MATCH (n) WHERE n:Blueprint OR n:KnowledgeChunk
WITH DISTINCT n.embedding_model AS model
WHERE model IS NOT NULL
RETURN collect(model) AS models
"""


def check_model_parity(existing_models: set[str], model_id: str) -> None:
    """Refuse a write that would mix embedding models into one index (§3.3).

    Pure function (the strict write-time guard, Layer-1-testable without infra):
    any already-stored model id other than *model_id* → `CorpusLoadError`. An
    empty-string stored stamp is treated as a CONFLICTING model, not a non-model
    (N2): a node with no model stamp is a broken row, not a free pass.
    """
    conflicting = {m for m in existing_models if m != model_id}
    if conflicting:
        raise CorpusLoadError(
            "Refusing to write model "
            f"{model_id!r} into an index already holding {sorted(conflicting)!r} "
            "(a mixed-model index is silently broken; reindex instead)."
        )


def _use_edges(uses: list[str]) -> list[dict[str, str]]:
    """Derive `{column_key, table_key}` edge rows from `"db.table.column"` keys.

    `table_key` is everything before the final dot (`"db.table"`), matching the
    scope-key construction `f"{db_table}.{column}"` (context/scope_filter).
    """
    edges: list[dict[str, str]] = []
    for key in uses:
        if "." not in key:
            continue  # malformed key carries no table grouping — skip the edge
        edges.append({"column_key": key, "table_key": key.rsplit(".", 1)[0]})
    return edges


def _validate_blueprint_uses(bp: BlueprintSeed) -> None:
    """Fail-closed with context on a malformed `uses` entry (S2 / §8).

    The design's own "highest-risk contract": a `uses` key that is not a byte-
    exact `"database.table.column"` scope key is silently dropped by the scope
    pre-filter at recall. Guard it at WRITE — every entry must be a `str` with at
    least 3 NON-EMPTY dot-separated parts, else raise with the offending key so
    an authoring mistake fails loudly instead of retrieving nothing.
    """
    for key in bp.uses:
        if not isinstance(key, str) or len(key.split(".")) < 3 or not all(key.split(".")):
            raise CorpusLoadError(
                f"blueprint {bp.id}: uses entry {key!r} is not a "
                "database.table.column scope key"
            )


async def apply_schema(driver: AsyncDriver, *, database: str = "neo4j") -> None:
    """Create the constraints + native vector indexes (idempotent), then wait
    for every index to come ONLINE so a subsequent recall sees them (§4.2)."""
    async with driver.session(database=database) as session:
        for statement in SCHEMA_STATEMENTS:
            await session.run(statement)  # type: ignore[arg-type]
        await session.run("CALL db.awaitIndexes(300)")  # type: ignore[arg-type]


async def load_corpus(
    driver: AsyncDriver,
    embedding_client: EmbeddingClient,
    blueprints: list[BlueprintSeed],
    knowledge: list[KnowledgeSeed],
    *,
    model_id: str,
    database: str = "neo4j",
    ensure_schema: bool = True,
) -> LoadReport:
    """Embed + upsert the seed corpus into neo4j (idempotent). See module docs.

    Raises `CorpusLoadError` on a malformed `uses` key (S2) or a write-time
    model-parity violation (§3.3).
    """
    if ensure_schema:
        await apply_schema(driver, database=database)

    # S2: validate the highest-risk contract BEFORE any embed/write — a malformed
    # scope key fails the whole load loudly rather than silently storing a
    # blueprint the scope filter will always drop.
    for bp in blueprints:
        _validate_blueprint_uses(bp)

    # Embed offline through the SAME endpoint the online path uses (parity by
    # construction). Order-preserving: `embed` returns one vector per input.
    bp_vectors = (
        await embedding_client.embed([b.intent for b in blueprints]) if blueprints else []
    )
    kn_vectors = (
        await embedding_client.embed([k.text for k in knowledge]) if knowledge else []
    )

    columns: set[str] = set()
    tables: set[str] = set()
    for bp in blueprints:
        for key in bp.uses:
            columns.add(key)
            tables.add(key.rsplit(".", 1)[0])

    async with driver.session(database=database) as session:

        async def _write(tx: AsyncManagedTransaction) -> None:
            # S3: the parity read + check are the FIRST statements of the write
            # transaction (not a separate auto-commit read), so two concurrent
            # seeders with different models cannot both pass-then-commit —
            # whichever commits second sees the first's stamp and is refused.
            existing = await _fetch_existing_models(tx)
            check_model_parity(existing, model_id)
            for bp, vector in zip(blueprints, bp_vectors, strict=True):
                await tx.run(
                    _UPSERT_BLUEPRINT,
                    id=bp.id,
                    intent=bp.intent,
                    slots_summary=bp.slots_summary,
                    embedding=vector,
                    model=model_id,
                    uses=list(bp.uses),
                    status=bp.status,
                    drift_status=bp.drift_status,
                    catalog_sha=bp.catalog_sha,
                )
                # S1: unconditional — rewrites the edge set (delete-then-add), so
                # a shrunk uses set leaves no phantom :USES edges.
                await tx.run(
                    _REWRITE_BLUEPRINT_EDGES, id=bp.id, use_edges=_use_edges(bp.uses)
                )
            for kn, vector in zip(knowledge, kn_vectors, strict=True):
                await tx.run(
                    _UPSERT_KNOWLEDGE,
                    id=kn.id,
                    title=kn.title,
                    text=kn.text,
                    embedding=vector,
                    model=model_id,
                    doc_id=kn.doc_id,
                    status=kn.status,
                )

        await session.execute_write(_write)
        # New nodes populate the vector index asynchronously — wait for it so a
        # caller (or the Layer-2 seed fixture) can recall immediately.
        await session.run("CALL db.awaitIndexes(300)")  # type: ignore[arg-type]

    report = LoadReport(
        model_id=model_id,
        blueprints_written=len(blueprints),
        knowledge_written=len(knowledge),
        columns_written=len(columns),
        tables_written=len(tables),
    )
    _logger.info("neo4j corpus load complete: %s", report)
    return report


async def _fetch_existing_models(runner: Any) -> set[str]:
    """Distinct non-null `embedding_model` stamps already in the index.

    *runner* is anything with `.run` — a session OR a managed transaction (S3
    calls this inside the write txn). Empty-string stamps are RETAINED (not
    filtered), so a broken/unstamped row surfaces as a parity conflict (N2)
    rather than a silent free pass; the Cypher already excludes true NULLs.
    """
    result = await runner.run(_EXISTING_MODELS)
    row = await result.single()
    if row is None:
        return set()
    return set(row["models"] or [])


__all__ = [
    "BlueprintSeed",
    "CorpusLoadError",
    "KnowledgeSeed",
    "LoadReport",
    "SCHEMA_STATEMENTS",
    "apply_schema",
    "check_model_parity",
    "load_corpus",
    "load_seed_fixtures",
]
