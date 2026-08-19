"""corpus_loader — offline write path for the neo4j retrieval corpus.

`load_corpus(...)` is a reusable, idempotent bulk upsert of a small, curated, TRUSTED seed:
it embeds every blueprint `intent` and knowledge `text` through the REAL embedding client
(parity with the online path by construction), MERGE-by-id upserts the nodes with the
embedding + `embedding_model` stamp + the denormalized `uses` property, and links each
blueprint's `:USES` edges to the PRE-EXISTING catalog `:Column` nodes in the SAME
transaction.

`resolve_blueprint_references(...)` runs FIRST in that pre-write pass: a `composes` node may
name another blueprint by id instead of carrying its own SQL, and that reference is resolved
and INLINED at load, so no reference id survives onto a node and the executor never resolves
one.

`load_catalog_graph(...)` is the separate, catalog-OWNED hydration of the `:Table`/`:Column`
graph from the MCP catalog EXPORT dict (no embeds, no model parity).

Parity is STRICT at write: the loader refuses to write two different embedding-model ids
into one index, because a mixed index is silently broken. Read-time parity is a DEGRADE
instead (`Neo4jVectorIndex` filters by `expected_model`, D86).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

# The seed DTOs + `CorpusLoadError` live in `data_agent.corpus.seeds` and are RE-EXPORTED
# from here (see `__all__`): one class object tree-wide, so every `except CorpusLoadError`
# still catches, and the learning plane can take the DTOs without importing this module.
from data_agent.corpus.seeds import (
    BlueprintSeed,
    CorpusLoadError,
    DimensionMismatchError,
    KnowledgeSeed,
    corpus_seeds_from_export,
)
from data_agent.runtime.blueprint.compiler import (
    dag_properties,
    resolve_blueprint_references,
    validate_blueprint_dag,
    validate_blueprint_uses,
)
from data_agent.runtime.retrieval.vector_index import READ_CORPUS_META_QUERY

if TYPE_CHECKING:
    from neo4j import AsyncDriver, AsyncManagedTransaction

    from data_agent.runtime.model.embedding_client import EmbeddingClient
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle

_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Load reports + seed fixtures (the seed DTOs themselves: `data_agent.corpus.seeds`)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadReport:
    """Outcome of a `load_corpus` run — counts plus the model the corpus was stamped with.

        `columns_referenced`/`tables_referenced` count the DISTINCT keys the seeded blueprints
        REFERENCE, not nodes this loader writes: `:Column`/`:Table` nodes are owned by
        `load_catalog_graph`, and `load_corpus` only links `:USES` to pre-existing ones.
    """

    model_id: str
    blueprints_written: int
    knowledge_written: int
    columns_referenced: int
    tables_referenced: int
    # Governed-corpus B1 no-op fast path (Phase 2), mirroring `CatalogGraphReport`:
    # `skipped=True` means the `:CorpusMeta` singleton already carried this run's
    # `corpus_sha`, so nothing was embedded or written. Defaulted so existing
    # (non-sha) callers — the landing writer, Layer-1 tests — are unaffected.
    skipped: bool = False


@dataclass(frozen=True)
class CatalogGraphReport:
    """Outcome of a `load_catalog_graph` run — the `:Table`/`:Column` hydration counts and the
        run's `catalog_sha`.

        `skipped=True` is the no-op fast path (the graph already carries this export's sha, so
        nothing was written). `drift_referenced_columns` lists any `:Column` the GC removed that
        STILL had an inbound `:USES` — GC wins, and the drift is logged.
    """

    catalog_sha: str
    skipped: bool
    tables_upserted: int
    columns_upserted: int
    tables_gc: int
    columns_gc: int
    drift_referenced_columns: tuple[str, ...]


# The default embedding dimension (all-mpnet-base-v2 → 768). Used when a caller
# needs a concrete dimension but the runtime cannot/should not infer one (e.g. the
# offline seed scripts + live integration tests wired to the 768 embedding mock).
# The runtime resolver (`resolve_embedding_dimension`) prefers the configured value
# or an inferred one; this constant is the last-resort literal for direct callers.
DEFAULT_EMBEDDING_DIMENSION = 768


def load_seed_fixtures(
    corpus_dir: Path | str,
) -> tuple[list[BlueprintSeed], list[KnowledgeSeed]]:
    """Read `blueprints.yaml` + `knowledge.yaml` under *corpus_dir* into seeds.

        Raises `CorpusLoadError` on a duplicate id, within OR across the two files: MERGE-by-id
        would otherwise let a copy-pasted id overwrite in place, masking an authoring mistake.
    """
    root = Path(corpus_dir)
    blueprints = [BlueprintSeed(**item) for item in _read_yaml_list(root / "blueprints.yaml")]
    knowledge = [KnowledgeSeed(**item) for item in _read_yaml_list(root / "knowledge.yaml")]
    _reject_duplicate_ids([b.id for b in blueprints] + [k.id for k in knowledge])
    return blueprints, knowledge


def effective_corpus_sha(export: dict[str, Any]) -> str:
    """The stamp/guard key for an online corpus hydration — the export's `blueprints_sha` +
        `knowledge_sha` combined, or a deterministic content hash when either is missing.

        A change to EITHER corpus flips the combined stamp, so the skip-guard and the GC re-run.
        An empty combined stamp would silently break both guards, hence the derived fallback.
    """
    bp_sha = export.get("blueprints_sha")
    kn_sha = export.get("knowledge_sha")
    if isinstance(bp_sha, str) and bp_sha and isinstance(kn_sha, str) and kn_sha:
        return f"{bp_sha}:{kn_sha}"
    payload = {"blueprints": export.get("blueprints") or {}, "knowledge": export.get("knowledge") or {}}
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    _logger.warning(
        "corpus export lacked blueprints_sha/knowledge_sha; using derived content hash "
        "%s (the B1 skip-guard + GC key off this stable digest)",
        digest,
    )
    return digest


def corpus_content_sha(
    blueprints: list[BlueprintSeed], knowledge: list[KnowledgeSeed]
) -> str:
    """A stable content-hash `corpus_sha` for a SEED-LIST reconcile. Deterministic over the
        seeds' full field content, so a re-seed of unchanged fixtures keeps the same stamp (an
        idempotent GC no-op) and any edit flips it.
    """
    from dataclasses import asdict

    payload = {
        "blueprints": sorted((asdict(b) for b in blueprints), key=lambda d: d["id"]),
        "knowledge": sorted((asdict(k) for k in knowledge), key=lambda d: d["id"]),
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _reject_duplicate_ids(ids: list[Any]) -> None:
    seen: set[str] = set()
    dupes: set[str] = set()
    for id_ in ids:
        # The membership test below needs a HASHABLE id, and these come straight from
        # YAML via `BlueprintSeed(**item)` — the dataclass declares `id: str` and
        # enforces nothing, so `id: [a, b]` in a fixture reaches `id_ in seen` and
        # raises a raw `TypeError: unhashable type: 'list'` one frame above the
        # `resolve_blueprint_references` guard that already refuses the same shape.
        # Only the CLI seed script reads fixtures (the hydrator loads the MCP export,
        # whose `_seed_from_entry` coerces the key with `str()`), so this cannot brick
        # the corpus — but it is the same class, and an unreadable traceback out of a
        # seed run is no better than a message that names the offending id.
        if not isinstance(id_, str) or not id_:
            raise CorpusLoadError(
                f"Seed id {id_!r} is not a non-empty string ({type(id_).__name__}) — "
                "ids key the corpus and every reference to it."
            )
        (dupes if id_ in seen else seen).add(id_)
    if dupes:
        raise CorpusLoadError(f"Duplicate seed id(s) in the corpus fixtures: {sorted(dupes)!r}.")


def _read_yaml_list(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or []
    if not isinstance(data, list):
        raise CorpusLoadError(f"Fixture {path} must be a YAML list.")
    return data


# --------------------------------------------------------------------------
# Schema DDL — idempotent constraints + native vector indexes (§1.4)
# --------------------------------------------------------------------------

# The catalog-graph constraints (Table/Column node keys + the :CatalogMeta
# singleton). Owned by `load_catalog_graph`'s lightweight `apply_catalog_graph_schema`
# (constraints ONLY — no vector-index DDL, no `awaitIndexes` on the cold-fetch turn),
# AND included in the full `schema_statements(dimension)` so `load_corpus` / the seed
# script self-deploy them too. Every statement is idempotent (`IF NOT EXISTS`).
_CATALOG_GRAPH_CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT column_key IF NOT EXISTS FOR (c:Column) REQUIRE c.key IS UNIQUE",
    "CREATE CONSTRAINT table_key IF NOT EXISTS FOR (t:Table) REQUIRE t.key IS UNIQUE",
    "CREATE CONSTRAINT catalog_meta_id IF NOT EXISTS FOR (m:CatalogMeta) REQUIRE m.id IS UNIQUE",
)

# The idempotent constraints (dimension-independent). Every statement is idempotent
# (`IF NOT EXISTS`) so `apply_schema` is safe to re-run.
_CORPUS_CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT blueprint_id IF NOT EXISTS FOR (b:Blueprint) REQUIRE b.id IS UNIQUE",
    "CREATE CONSTRAINT knowledge_id IF NOT EXISTS FOR (k:KnowledgeChunk) REQUIRE k.id IS UNIQUE",
    # Governed-corpus B1 freshness singleton (Phase 2), mirroring `:CatalogMeta`.
    "CREATE CONSTRAINT corpus_meta_id IF NOT EXISTS FOR (m:CorpusMeta) REQUIRE m.id IS UNIQUE",
    *_CATALOG_GRAPH_CONSTRAINTS,
)


def schema_statements(dimension: int) -> tuple[str, ...]:
    """The full idempotent schema DDL (constraints + the two native vector indexes), with the
        vector-index dimension parameterized.

        The dimension is resolved from settings or INFERRED from the live embedder, so a
        different embedding model is honored without a code edit. Every statement is
        `IF NOT EXISTS`, so the whole write path is re-runnable — BUT a
        `CREATE VECTOR INDEX ... IF NOT EXISTS` silently keeps the OLD dimension of a
        pre-existing index, which is why `apply_schema` introspects and raises before creating.
    """
    return (
        *_CORPUS_CONSTRAINTS,
        # PriorArt Slice 2 — the LOOSE cross-tier key's lookup index. The learning
        # loop's `PriorArtIndex.get_by_structural_key` matches a new candidate against
        # every tier by this key; without a RANGE index that MATCH is a
        # `NodeByLabelScan` over every :Blueprint. Cheap at 12 nodes and quietly linear
        # at 12,000, so it goes in with the query rather than after someone notices.
        # A plain (non-unique) index deliberately: two tiers legitimately carry the SAME
        # structural key — a canon blueprint and the learning node that re-derived it —
        # which is precisely the collision the key exists to detect, so a UNIQUE
        # constraint here would refuse to land the very thing we want to find.
        # (`EXPLAIN` against the live graph: `NodeIndexSeek`, not `NodeByLabelScan`.)
        "CREATE INDEX blueprint_structural_key IF NOT EXISTS "
        "FOR (b:Blueprint) ON (b.structural_key)",
        "CREATE VECTOR INDEX blueprint_intent_vec IF NOT EXISTS "
        "FOR (b:Blueprint) ON (b.intent_embedding) "
        f"OPTIONS {{ indexConfig: {{ `vector.dimensions`: {dimension}, "
        "`vector.similarity_function`: 'cosine' } }",
        "CREATE VECTOR INDEX knowledge_text_vec IF NOT EXISTS "
        "FOR (k:KnowledgeChunk) ON (k.text_embedding) "
        f"OPTIONS {{ indexConfig: {{ `vector.dimensions`: {dimension}, "
        "`vector.similarity_function`: 'cosine' } }",
    )


# --------------------------------------------------------------------------
# Upsert Cypher (§3.1) — MERGE-by-id so a re-run updates in place, never dupes
# --------------------------------------------------------------------------

_UPSERT_BLUEPRINT = """
MERGE (b:Blueprint {id: $id})
SET b.name = $id,
    b.intent = $intent,
    b.slots_summary = $slots_summary,
    b.intent_embedding = $embedding,
    b.embedding_model = $model,
    b.uses = $uses,
    b.status = $status,
    b.drift_status = $drift_status,
    b.catalog_sha = $catalog_sha,
    b.created_by = $created_by,
    b.source_candidate_id = $source_candidate_id,
    b.source = $source,
    b.verified = $verified,
    b.corpus_sha = $corpus_sha,
    b.created_at = coalesce(b.created_at, datetime()),
    b.hit_count = coalesce(b.hit_count, 0),
    b.resolves_json = $resolves_json,
    b.slots_json = $slots_json,
    b.uses_rules_json = $uses_rules_json,
    b.sql_template = $sql_template,
    b.composes_json = $composes_json,
    b.result_grain_json = $result_grain_json,
    b.window_anchor = $window_anchor,
    b.structural_key = $structural_key,
    b.structural_key_recipe = $structural_key_recipe
"""

# Reserved graph shape (§1.3): the blueprint's USES closure written as edges,
# same-txn with the denormalized `uses` property so they cannot drift. Unread at
# recall in Slice 2. S1: DELETE this blueprint's existing :USES edges FIRST, so a
# re-seed with a SHRUNK uses set leaves no phantom edges (MERGE alone never
# removes stale edges). Runs unconditionally per blueprint (even when the new
# use-set is empty), so the DELETE always clears stale edges.
#
# MERGE→MATCH (catalog-graph hydration): the `:Column`/`:Table` nodes are now
# OWNED by `load_catalog_graph` (enriched + self-healing), not minted here. This
# rewrite OPTIONAL-MATCHes a PRE-EXISTING catalog `:Column` and creates the `:USES`
# edge only when it exists; it never mints a `:Column`/`:Table` and never writes
# `:OF_TABLE` (now catalog-owned). Column keys the blueprint references but the
# catalog does not carry are COLLECTED and RETURNed so `load_corpus._write` can log
# a structured blueprint→column drift warning (a blueprint referencing a column the
# catalog never advertised — the edge is silently absent, so surface it).
_REWRITE_BLUEPRINT_EDGES = """
MATCH (b:Blueprint {id: $id})
OPTIONAL MATCH (b)-[r:USES]->()
DELETE r
WITH DISTINCT b
UNWIND $use_edges AS ue
OPTIONAL MATCH (c:Column {key: ue.column_key})
FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | MERGE (b)-[:USES]->(c))
WITH ue.column_key AS column_key, c
WHERE c IS NULL
RETURN collect(column_key) AS missing
"""

# --------------------------------------------------------------------------
# Catalog-graph hydration Cypher (independent, enriched, self-healing) — the
# `:Table`/`:Column` nodes are catalog-OWNED, upserted from the MCP export dict,
# stamped with the run's `catalog_sha`, and GC'd when a prior run's stamp is stale.
# All node props are primitive/array-of-primitive (nested maps JSON-encoded to
# `*_json` strings), so `SET x += row.props` is always a valid Neo4j write.
# --------------------------------------------------------------------------

# Batched table upsert: MERGE by key, overwrite the enriched props, stamp the sha.
_UPSERT_TABLES = """
UNWIND $rows AS row
MERGE (t:Table {key: row.key})
SET t += row.props, t.catalog_sha = $sha
"""

# Batched column upsert: MERGE the column, overwrite props + stamp sha, and MERGE
# the `:OF_TABLE` edge to its (already-upserted) owning table (catalog-owned here).
_UPSERT_COLUMNS = """
UNWIND $rows AS row
MERGE (c:Column {key: row.key})
SET c += row.props, c.catalog_sha = $sha
MERGE (t:Table {key: row.table_key})
MERGE (c)-[:OF_TABLE]->(t)
"""

# GC columns not touched by THIS run (stamped sha != run sha, OR never stamped —
# a legacy MERGE-minted phantom from before catalog ownership). Before deleting,
# capture any inbound `:USES` blueprint ids (a blueprint referencing a column the
# catalog just dropped → drift). `key`/`refs` are materialized in WITH BEFORE the
# DETACH DELETE so they can be RETURNed (a deleted node's props are unreadable).
_GC_COLUMNS = """
MATCH (c:Column) WHERE coalesce(c.catalog_sha, '') <> $sha
OPTIONAL MATCH (b:Blueprint)-[:USES]->(c)
WITH c, c.key AS key, collect(b.id) AS refs
DETACH DELETE c
RETURN key, refs
"""

# GC tables not touched by this run (same stale-stamp rule; no inbound :USES to
# capture — only columns carry blueprint references). Returns the delete count.
_GC_TABLES = """
MATCH (t:Table) WHERE coalesce(t.catalog_sha, '') <> $sha
DETACH DELETE t
RETURN count(*) AS deleted
"""

# `:CatalogMeta` singleton — the process-wide freshness stamp. The read powers the
# B1 no-op fast path (skip hydration when the export sha already matches); the
# upsert lands the new sha atomically with the node upserts + GC in one txn.
_READ_CATALOG_META = """
MATCH (m:CatalogMeta {id: 'singleton'}) RETURN m.catalog_sha AS catalog_sha
"""

_UPSERT_CATALOG_META = """
MERGE (m:CatalogMeta {id: 'singleton'})
SET m.catalog_sha = $sha
"""

_UPSERT_KNOWLEDGE = """
MERGE (k:KnowledgeChunk {id: $id})
SET k.name = $id,
    k.title = $title,
    k.text = $text,
    k.text_embedding = $embedding,
    k.embedding_model = $model,
    k.doc_id = $doc_id,
    k.status = $status,
    k.drift_status = $drift_status,
    k.created_by = $created_by,
    k.source_candidate_id = $source_candidate_id,
    k.source = $source,
    k.verified = $verified,
    k.corpus_sha = $corpus_sha,
    k.created_at = coalesce(k.created_at, datetime())
"""

# SCOPED to the TRUSTED `source='mcp'` partition (hydrator redesign). Model parity
# governs the RECALLABLE partition only: recall filters `WHERE embedding_model=<expected>`
# AND `source='mcp'`, so a `source='learning'` staging node lagging the embedding model is
# harmless (it never matches recall's model filter, and gets a fresh embedding only if/when
# it is promoted). Counting learning nodes here would falsely trip parity on a model swap and
# wedge the hydrator. `n.source = 'mcp'` is BARE equality (fail-closed: a sourceless node is
# NOT counted as mcp) mirroring the recall trust gate.
_EXISTING_MODELS = """
MATCH (n) WHERE (n:Blueprint OR n:KnowledgeChunk) AND n.source = 'mcp'
WITH DISTINCT n.embedding_model AS model
WHERE model IS NOT NULL
RETURN collect(model) AS models
"""

# Dimension-parity introspection (Part B): the CONFIGURED dimension of the two
# corpus vector indexes, read from the index options. A `CREATE VECTOR INDEX ...
# IF NOT EXISTS` silently keeps the OLD dimension, so this is the ONLY way to detect
# that the embedding model/dimension changed under an existing index — `apply_schema`
# runs it BEFORE the create and raises `DimensionMismatchError` on a differing dim.
_EXISTING_VECTOR_DIMS = """
SHOW VECTOR INDEXES YIELD name, options
WHERE name IN ['blueprint_intent_vec', 'knowledge_text_vec']
RETURN name, options['indexConfig']['vector.dimensions'] AS dimensions
"""

# --------------------------------------------------------------------------
# Governed-corpus reconcile Cypher (Phase 2) — mirror of the catalog-graph
# self-healing pattern, but stamping/keying on `corpus_sha` and SCOPED to the
# TRUSTED `source='mcp'` partition. `load_corpus` stamps each seeded mcp node with
# the run's `corpus_sha`; on `gc=True` (the explicit seed/reconcile op) the GC
# deletes any `source='mcp'` node a newer run no longer touched (stale stamp).
# --------------------------------------------------------------------------

# `:CorpusMeta` singleton — the process-wide corpus freshness stamp. Read powers the
# B1 no-op fast path (skip embed+write when the sha already matches); the upsert lands
# the new sha atomically with the node upserts + GC in one txn. Mirrors `:CatalogMeta`.
#
# The READ is shared with `vector_index`'s `/ready` graph-readiness probe (see that
# module for why it is the definition site) — the writer here and the probe there must
# key the singleton identically or `/ready` reports a corpus this loader considers
# unseeded, or vice versa. Only the read is shared: this module is the sole WRITER.
_READ_CORPUS_META = READ_CORPUS_META_QUERY

_UPSERT_CORPUS_META = """
MERGE (m:CorpusMeta {id: 'singleton'})
SET m.corpus_sha = $corpus_sha
"""

# GC trusted blueprints/knowledge NOT touched by THIS run (stamped corpus_sha !=
# run sha, OR never stamped). The `node.source = 'mcp'` guard is SAFETY-CRITICAL and
# NON-NEGOTIABLE: it is BARE equality, so the GC can NEVER match — and therefore never
# DETACH DELETE — a `source='learning'` staging node (or any node with no `source`).
# The learning tier is invisible to reconcile; only the MCP-canon projection is
# self-healed. `coalesce(node.corpus_sha,'')` treats an unstamped mcp node as stale
# (a legacy/broken row) so a stale-sha run reaps it.
_GC_BLUEPRINTS = """
MATCH (b:Blueprint)
WHERE b.source = 'mcp' AND coalesce(b.corpus_sha, '') <> $corpus_sha
DETACH DELETE b
RETURN count(*) AS deleted
"""

_GC_KNOWLEDGE = """
MATCH (k:KnowledgeChunk)
WHERE k.source = 'mcp' AND coalesce(k.corpus_sha, '') <> $corpus_sha
DETACH DELETE k
RETURN count(*) AS deleted
"""


def check_model_parity(existing_models: set[str], model_id: str) -> None:
    """Refuse a write that would mix embedding models into one index.

        Pure (the strict write-time guard, Layer-1-testable without infra): any already-stored
        model id other than *model_id* raises `CorpusLoadError`. An empty-string stored stamp is
        treated as a CONFLICTING model, not a non-model — a node with no model stamp is a broken
        row, not a free pass.
    """
    conflicting = {m for m in existing_models if m != model_id}
    if conflicting:
        raise CorpusLoadError(
            "Refusing to write model "
            f"{model_id!r} into an index already holding {sorted(conflicting)!r} "
            "(a mixed-model index is silently broken; reindex instead)."
        )


def check_dimension_parity(existing_dims: set[int], target_dim: int) -> None:
    """Refuse to (re)deploy the schema when a pre-existing vector index carries a DIFFERENT
        embedding dimension than *target_dim*.

        Pure. *existing_dims* is the set of `vector.dimensions` read from `SHOW VECTOR INDEXES`;
        an empty set (no index yet) passes trivially and the create runs at *target_dim*. Any
        other dimension raises `DimensionMismatchError` naming BOTH dims, because a
        `CREATE ... IF NOT EXISTS` would silently keep the old one and recall would break
        undetectably.
    """
    conflicting = {d for d in existing_dims if d != target_dim}
    if conflicting:
        raise DimensionMismatchError(
            f"A corpus vector index already exists at dimension(s) {sorted(conflicting)!r} "
            f"but this run targets dimension {target_dim}. `CREATE VECTOR INDEX ... IF NOT "
            "EXISTS` silently keeps the OLD dimension, so the index cannot be reshaped in "
            "place. The embedding model/dimension changed: the singleton hydrator daemon "
            "nukes + rebuilds the graph at the new dimension (dropping + recreating the "
            "vector indexes) — or drop the stale indexes manually."
        )


async def fetch_existing_vector_dims(runner: Any) -> set[int]:
    """The set of `vector.dimensions` configured on the two corpus vector indexes.

        *runner* is anything with `.run` (a live session OR a recording stub). Absent indexes
        yield an empty set — a fresh graph, so the create runs at the target dim. A `None` or
        non-int dimension row is skipped defensively.
    """
    result = await runner.run(_EXISTING_VECTOR_DIMS)
    rows = await result.data()
    dims: set[int] = set()
    for row in rows:
        raw = row.get("dimensions")
        if isinstance(raw, int) and not isinstance(raw, bool):
            dims.add(raw)
    return dims


async def resolve_embedding_dimension(
    embedding_client: EmbeddingClient,
    *,
    configured: int | None = None,
    sample_vectors: list[list[float]] | None = None,
) -> int:
    """Resolve the vector-index dimension: the CONFIGURED value when set, else INFERRED from
        already-embedded *sample_vectors* (no extra network call), else a one-shot PROBE embed.

        An empty or degenerate probe result raises `CorpusLoadError` — the schema cannot be
        shaped without a dimension.

        When BOTH a configured value AND a non-empty sample vector are present, the sample's
        length MUST equal the configured one: otherwise the operator set `EMBEDDING_DIMENSION` to
        a value the live model does NOT emit, which would build the index at one dimension, write
        vectors of another, and silently EXCLUDE every row from the index.
    """
    first_sample = next((vec for vec in sample_vectors or [] if vec), None)
    if configured is not None:
        if first_sample is not None and len(first_sample) != configured:
            raise CorpusLoadError(
                f"configured embedding_dimension={configured} does NOT match the live "
                f"embedder's vector length {len(first_sample)} — the index would be built "
                f"at {configured} while {len(first_sample)}-dim vectors are written and "
                "silently excluded from recall. Fix EMBEDDING_DIMENSION or the model."
            )
        return configured
    if first_sample is not None:
        return len(first_sample)
    probe = await embedding_client.embed(["__dimension_probe__"])
    if not probe or not probe[0]:
        raise CorpusLoadError(
            "cannot resolve the embedding dimension — the embedder returned no/empty "
            "vector for the probe (set RuntimeSettings.embedding_dimension explicitly)."
        )
    return len(probe[0])


def _use_edges(uses: list[str]) -> list[dict[str, str]]:
    """Derive `{column_key, table_key}` edge rows from `"db.table.column"` keys.

        `table_key` is everything before the final dot, matching the scope-key construction
        `f"{db_table}.{column}"`.
    """
    edges: list[dict[str, str]] = []
    for key in uses:
        if "." not in key:
            continue  # malformed key carries no table grouping — skip the edge
        edges.append({"column_key": key, "table_key": key.rsplit(".", 1)[0]})
    return edges


# --------------------------------------------------------------------------
# Catalog-graph pure helpers (Layer-1 testable, no infra) — project one MCP
# catalog EXPORT entry into the enriched `:Table`/`:Column` node props. Neo4j
# props must be primitive/array-of-primitive, so every nested map/list-of-map is
# JSON-encoded to a `*_json` string (mirroring the blueprint DAG `*_json` pattern);
# `catalog_sha` is stamped separately in Cypher (`SET x.catalog_sha = $sha`), not
# carried in `props`. Fields are WHITELISTED — the entry is never blindly spread.
# --------------------------------------------------------------------------


def _json_or_none(value: Any) -> str | None:
    """JSON-encode a nested map/list value, or `None` when empty/absent — mirrors
    `dag_properties` so an absent structure carries no phantom `{}`/`[]` and a
    re-seed with the value removed clears the stale `*_json` prop (null `+=` removes)."""
    return json.dumps(value) if value else None


def _str_list(value: Any) -> list[str]:
    """Coerce a value into a list of strings (dropping a non-list to `[]`), for the
        array-of-primitive node props (`grain`, `synonyms`). Casing is preserved (D70).

        TODO (cleanup wave B): replace with `data_agent.untrusted.as_str_list`. This is the WEAK
        copy — `str(item)` writes the literal `"None"` for a JSON null and a dict member lands as
        its repr, both of which then read as real grain/synonym content, whereas the shared
        coercer SKIPS non-`str` members. The change is behavioural, so it belongs in a slice that
        owns this file.
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _table_node_props(db_table: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Project one catalog entry into the enriched `:Table` node props.

        `key` is `db_table`, byte-identical to `_use_edges`' `table_key`. Scalars and arrays are
        stored natively; the nested fields are JSON-encoded to `*_json`. `grain_verifiable`
        defaults True when absent (parity with `SemanticCatalogHandle`). `catalog_sha` is NOT
        included here — the upsert Cypher stamps it.
    """
    default_db, _, default_table = db_table.partition(".")
    grain_verifiable = entry.get("grain_verifiable", True)
    if not isinstance(grain_verifiable, bool):
        grain_verifiable = True
    return {
        "key": db_table,
        # `name` mirrors `key` (the node identity) so Neo4j Browser/Bloom caption the
        # node by its `db.table` key — consistent with :Blueprint/:KnowledgeChunk/:Column.
        "name": db_table,
        "database": entry.get("database") or default_db,
        "table": entry.get("table") or default_table,
        "description": entry.get("description"),
        "grain": _str_list(entry.get("grain")),
        "grain_verifiable": grain_verifiable,
        "temporal_json": _json_or_none(entry.get("temporal")),
        "primary_key_json": _json_or_none(entry.get("primary_key")),
        "join_keys_json": _json_or_none(entry.get("join_keys")),
        "measures_json": _json_or_none(entry.get("measures")),
    }


def _column_node_props(db_table: str, name: str, col: dict[str, Any]) -> dict[str, Any]:
    """Project one catalog column into the enriched `:Column` node props.

        `key` is `f"{db_table}.{name}"` — byte-identical to `_use_edges`' `column_key`, asserted
        by a unit test. `name` mirrors the FULL key so graph browsers caption the column by it,
        consistently with the other node types; the bare short name is retained as `short_name`.
        Nested `values` is JSON-encoded; unknown or adversarial extra keys are simply not read.
    """
    key = f"{db_table}.{name}"
    return {
        "key": key,
        "name": key,
        "short_name": name,
        "type": col.get("type"),
        "description": col.get("description"),
        "sensitive": bool(col.get("sensitive", False)),
        "description_col": col.get("description_col"),
        "synonyms": _str_list(col.get("synonyms")),
        "unit": col.get("unit"),
        "values_json": _json_or_none(col.get("values")),
    }


def _catalog_graph_rows(
    catalog: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], set[str]]:
    """Build the batched UNWIND rows for the `:Table`/`:Column` upserts from a parsed catalog
        dict (`{db.table: <entry>}`).

        Returns `(table_rows, column_rows, table_keys, column_keys)`. A non-dict entry, or a
        non-dict column def, is tolerated — skipped, or falling back to `{}` — so a mangled
        fixture never crashes the build and just yields sparse props via the whitelist.
    """
    table_rows: list[dict[str, Any]] = []
    column_rows: list[dict[str, Any]] = []
    table_keys: set[str] = set()
    column_keys: set[str] = set()
    for db_table, entry in catalog.items():
        if not isinstance(entry, dict):
            continue
        table_rows.append({"key": db_table, "props": _table_node_props(db_table, entry)})
        table_keys.add(db_table)
        columns = entry.get("columns")
        if not isinstance(columns, dict):
            continue
        for name, col in columns.items():
            col_dict = col if isinstance(col, dict) else {}
            col_key = f"{db_table}.{name}"
            column_rows.append(
                {
                    "key": col_key,
                    "table_key": db_table,
                    "props": _column_node_props(db_table, name, col_dict),
                }
            )
            column_keys.add(col_key)
    return table_rows, column_rows, table_keys, column_keys


def _referenced_gc_columns(gc_rows: list[dict[str, Any]]) -> tuple[str, ...]:
    """From the `_GC_COLUMNS` result rows (`{key, refs}`), the sorted set of GC'd
    column keys that STILL had ≥1 inbound `:USES` — a blueprint referencing a column
    the catalog just dropped (drift). GC wins (the column is deleted anyway)."""
    return tuple(sorted(str(row["key"]) for row in gc_rows if row.get("refs")))


def _format_edge_drift(missing_by_blueprint: dict[str, list[str]]) -> str:
    """Render the blueprint→column edge-drift map (blueprint id → missing column
    keys) as a stable, sorted string for the structured warning log."""
    return "; ".join(
        f"{bp_id}: {sorted(keys)}" for bp_id, keys in sorted(missing_by_blueprint.items())
    )


def _warn_on_catalog_skew(blueprints: list[BlueprintSeed], catalog: CatalogHandle) -> None:
    """Log a SOFT WARNING per blueprint whose `uses` references a `db.table` absent from
        *catalog*. Never raises: a blueprint may legitimately reference tables absent from a
        partial or dev catalog snapshot, so this is a dev-time early warning for catalog and
        extractor skew, not a load precondition.

        The `db.table` grouping is everything-before-the-final-dot — the SAME convention as
        `_use_edges`' `table_key` and the scope-key construction — so the two parsers agree even
        for a key with a dotted table segment.
    """
    for bp in blueprints:
        missing: list[str] = []
        seen: set[str] = set()
        for key in bp.uses:
            db_table = key.rsplit(".", 1)[0]  # matches _use_edges' table_key
            if db_table in seen:
                continue
            seen.add(db_table)
            database, table = db_table.split(".", 1)
            if not catalog.is_catalogued(database, table):
                missing.append(db_table)
        if missing:
            _logger.warning(
                "blueprint %s: uses %d table(s) absent from the supplied catalog: %s "
                "(catalog/extractor skew — may strand an ok+None result at runtime)",
                bp.id,
                len(missing),
                ", ".join(missing),
            )


async def apply_schema(
    driver: AsyncDriver, *, dimension: int, database: str = "neo4j"
) -> None:
    """Create the constraints + native vector indexes (idempotent, at *dimension*), then wait
        for every index to come ONLINE so a subsequent recall sees them.

        BEFORE the `CREATE VECTOR INDEX ... IF NOT EXISTS` — which silently keeps a pre-existing
        index's OLD dimension — the existing dimensions are introspected and checked, raising
        `DimensionMismatchError` when one already exists at a DIFFERENT dimension (the signal
        that the embedding model changed and the graph must be rebuilt). A fresh graph passes and
        the indexes are created at *dimension*.
    """
    async with driver.session(database=database) as session:
        existing_dims = await fetch_existing_vector_dims(session)
        check_dimension_parity(existing_dims, dimension)
        for statement in schema_statements(dimension):
            await session.run(statement)  # type: ignore[arg-type]
        await session.run("CALL db.awaitIndexes(300)")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Nuke + rebuild — DESTRUCTIVE: drop the whole graph so it can be rebuilt from the
# LIVE MCP at a (possibly new) embedding dimension. Owned by the singleton hydrator
# daemon (retrieval/hydrator.py), which nukes on a DimensionMismatchError and re-seeds.
# --------------------------------------------------------------------------

# The COMPLETE object set the nuke drops before `DETACH DELETE`: the 2 vector
# indexes (REQUIRED so a new-dimension index can be created — a `CREATE ... IF NOT
# EXISTS` would otherwise keep the old dimension) and the 6 schema constraints (so a
# fresh `apply_schema`/`apply_catalog_graph_schema` re-creates them cleanly). Every
# statement is `IF EXISTS`, so the nuke is safe on a partially-provisioned graph.
_NUKE_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX blueprint_intent_vec IF EXISTS",
    "DROP INDEX knowledge_text_vec IF EXISTS",
    "DROP CONSTRAINT blueprint_id IF EXISTS",
    "DROP CONSTRAINT knowledge_id IF EXISTS",
    "DROP CONSTRAINT corpus_meta_id IF EXISTS",
    "DROP CONSTRAINT column_key IF EXISTS",
    "DROP CONSTRAINT table_key IF EXISTS",
    "DROP CONSTRAINT catalog_meta_id IF EXISTS",
)

# Clears EVERY node (and its relationships) EXCEPT the `:RebuildLock` singleton,
# CRUCIALLY including the `:CatalogMeta`/`:CorpusMeta {id:'singleton'}` freshness
# singletons — if those survive, the B1 no-op guards (`_read_catalog_meta`/
# `_read_corpus_meta`) would short-circuit and SKIP the re-seed, leaving an empty graph.
# The `:RebuildLock` is DELIBERATELY spared (`WHERE NOT n:RebuildLock`): if the nuke
# deleted its own lock, a replica booting during another's rebuild (the widest window —
# MCP fetch + full-corpus embed) would see no lock, claim fresh, and nuke AGAIN, tearing
# the graph the B1 guards then certify as healthy. Sparing the lock protects the FULL
# rebuild plus the `$stale_seconds` window (and makes a restart-with-the-flag-still-on
# within that window a skip instead of a re-nuke).
_NUKE_DELETE_NODES = "MATCH (n) WHERE NOT n:RebuildLock DETACH DELETE n"

# A uniqueness constraint on the lock key so a simultaneous double-MERGE from two
# concurrent-boot replicas cannot double-create the singleton (closing the last
# single-flight hole). Created inside `claim_rebuild_lock` BEFORE the MERGE, and
# deliberately NOT dropped by `_NUKE_STATEMENTS` (the lock + its keying must survive
# the nuke that spares the lock node).
_REBUILD_LOCK_CONSTRAINT = (
    "CREATE CONSTRAINT rebuild_lock_id IF NOT EXISTS "
    "FOR (l:RebuildLock) REQUIRE l.id IS UNIQUE"
)

# DEAD for the hydrator (retained for the seed script + back-compat): the hydrator is a
# `replicas:1` SINGLETON, so it owns the graph write path alone and needs NO distributed
# single-flight lock — it never calls `claim_rebuild_lock`, and the `:RebuildLock` sparing
# in `_NUKE_DELETE_NODES` is moot for it. The lock (and `nuke_graph`) stay in the module
# for `scripts/seed_neo4j_corpus.py` and any future multi-writer maintenance op.
#
# Best-effort single-flight claim: a `RebuildLock` singleton CAS so concurrent replicas
# don't all nuke on boot. The claim is Cypher-side (Neo4j
# `datetime()`, since the Python runtime has no cheap clock to embed) — ON CREATE the
# caller claims; ON MATCH the caller re-claims ONLY if the prior claim is absent or
# STALE (older than `$stale_seconds`, so a crashed holder can't wedge the lock
# forever). `claimed` is True iff THIS `$holder` now owns the lock. The nuke SPARES this
# node (`_NUKE_DELETE_NODES`), so the lock protects the full rebuild + the stale window.
_CLAIM_REBUILD_LOCK = """
MERGE (l:RebuildLock {id: 'singleton'})
ON CREATE SET l.holder = $holder, l.claimed_at = datetime()
ON MATCH SET
    l.holder = CASE
        WHEN l.claimed_at IS NULL
             OR datetime() > l.claimed_at + duration({seconds: $stale_seconds})
        THEN $holder ELSE l.holder END,
    l.claimed_at = CASE
        WHEN l.claimed_at IS NULL
             OR datetime() > l.claimed_at + duration({seconds: $stale_seconds})
        THEN datetime() ELSE l.claimed_at END
RETURN l.holder = $holder AS claimed
"""


async def claim_rebuild_lock(
    driver: AsyncDriver,
    *,
    holder: str,
    stale_seconds: int = 300,
    database: str = "neo4j",
) -> bool:
    """Best-effort single-flight claim on the `RebuildLock` singleton.

        Returns True iff THIS *holder* now owns the lock and may nuke + rebuild; False iff
        another live holder holds a fresh claim. The claim protects the FULL rebuild plus the
        stale window, and the `rebuild_lock_id` uniqueness constraint created here closes the
        simultaneous double-MERGE. A stale claim (holder crashed) is reclaimed after
        *stale_seconds*.
    """
    async with driver.session(database=database) as session:
        await session.run(_REBUILD_LOCK_CONSTRAINT)  # type: ignore[arg-type]
        result = await session.run(
            _CLAIM_REBUILD_LOCK, holder=holder, stale_seconds=stale_seconds
        )
        row = await result.single()
    return bool(row["claimed"]) if row is not None else False


async def nuke_graph(driver: AsyncDriver, *, database: str = "neo4j") -> None:
    """DESTRUCTIVE: drop the corpus vector indexes + all schema constraints, then
        `DETACH DELETE` every node, leaving an empty graph ready for a fresh `apply_schema` and
        reseed at a possibly new embedding dimension.

        Dropping the vector indexes is REQUIRED (a `CREATE ... IF NOT EXISTS` keeps the old
        dimension otherwise); deleting the nodes is REQUIRED so the freshness singletons do not
        short-circuit the re-seed. The `:RebuildLock` singleton is SPARED so the single-flight
        guard survives its own nuke.

        Strictly the seed-script maintenance op. The SINGLETON hydrator does NOT call this — it
        would DESTROY the `source='learning'` staging tier, which is not in the MCP export and
        would not be re-seeded; the hydrator uses `rebuild_mcp_corpus_partition` instead.
    """
    async with driver.session(database=database) as session:
        for statement in _NUKE_STATEMENTS:
            await session.run(statement)  # type: ignore[arg-type]
        await session.run(_NUKE_DELETE_NODES)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Scoped mcp-partition rebuild (the SINGLETON hydrator's destructive op) — clears ONLY
# the trusted `source='mcp'` corpus + the freshness singletons, PRESERVING the
# `source='learning'` staging tier (human-promoted content, absent from the MCP export and
# not re-seeded) and the whole `:Table`/`:Column` catalog graph. The data-loss-safe
# replacement for `nuke_graph` on an automatic model/dimension change.
# --------------------------------------------------------------------------

# DELETE only the TRUSTED corpus partition — BARE `source = 'mcp'` equality (fail-closed:
# a sourceless/learning node is never matched, so the learning tier survives).
_DELETE_MCP_CORPUS_NODES = """
MATCH (n) WHERE (n:Blueprint OR n:KnowledgeChunk) AND n.source = 'mcp'
DETACH DELETE n
"""

# DELETE the B1 freshness singletons so the subsequent reseed does NOT short-circuit on a
# stale sha (`load_catalog_graph`/`load_corpus` both re-run + re-stamp). `:Table`/`:Column`
# are untouched — the catalog reseed re-upserts + re-stamps them (no data lost).
_DELETE_FRESHNESS_SINGLETONS = """
MATCH (m) WHERE m:CorpusMeta OR m:CatalogMeta
DETACH DELETE m
"""


async def rebuild_mcp_corpus_partition(
    driver: AsyncDriver, *, dimension: int | None = None, database: str = "neo4j"
) -> None:
    """Scoped destructive reseed-prep for the SINGLETON hydrator (data-loss-safe).

        Clears ONLY the trusted `source='mcp'` nodes plus the `:CorpusMeta`/`:CatalogMeta`
        freshness singletons, then leaves the caller to reseed. PRESERVES the `source='learning'`
        staging tier (human-promoted content not in the MCP export, which a full `nuke_graph`
        would destroy) AND the `:Table`/`:Column` catalog graph.

        Two modes:
          * *dimension* given (a DIMENSION change) — additionally DROP and recreate the two
            corpus vector indexes at the new dimension, since a `CREATE ... IF NOT EXISTS`
            silently keeps the old one. The preserved learning-tier nodes keep their old-dim
            embeddings and are excluded from both the new-dim index and the recall source gate,
            so no bad neighbours surface.
          * *dimension* `None` (a same-dim MODEL swap) — leave the indexes; just clear the mcp
            nodes so the reseed re-embeds them at the new model without tripping the mcp-scoped
            write-time model-parity guard, which reads the pre-write state.

        Deleting the mcp nodes rather than overwriting them is REQUIRED even for a same-dim swap:
        the parity check reads the existing mcp models as the FIRST statement of the write txn, so
        a stale old-model node would raise `CorpusLoadError` before the MERGE-by-id overwrite ran.

        ATOMICITY: the mcp-node delete AND the freshness-singleton delete run in ONE
        `execute_write` so they commit together. As two auto-commit statements, a crash between
        them could leave the mcp partition DELETED while `:CorpusMeta` SURVIVED at its old sha —
        the next cycle would see no model change, sha-SKIP, and recall would be permanently empty
        while /ready still read 200. The vector-index DDL stays OUTSIDE the txn (neo4j forbids
        schema ops inside a data txn).
    """
    async with driver.session(database=database) as session:
        if dimension is not None:
            # Drop the vector indexes so they can be recreated at the new dimension.
            # Schema DDL must run OUTSIDE the data txn below (neo4j forbids it inside one).
            await session.run("DROP INDEX blueprint_intent_vec IF EXISTS")  # type: ignore[arg-type]
            await session.run("DROP INDEX knowledge_text_vec IF EXISTS")  # type: ignore[arg-type]

        async def _clear(tx: AsyncManagedTransaction) -> None:
            # BOTH deletes in ONE txn — commit-together atomicity (see docstring).
            await tx.run(_DELETE_MCP_CORPUS_NODES)
            await tx.run(_DELETE_FRESHNESS_SINGLETONS)

        await session.execute_write(_clear)

        if dimension is not None:
            for statement in schema_statements(dimension):
                await session.run(statement)  # type: ignore[arg-type]
            await session.run("CALL db.awaitIndexes(300)")  # type: ignore[arg-type]


async def apply_catalog_graph_schema(driver: AsyncDriver, *, database: str = "neo4j") -> None:
    """Ensure ONLY the catalog-graph constraints (`Table.key`, `Column.key`, `CatalogMeta.id`)
        — the lightweight schema-ensure `load_catalog_graph` uses.

        Deliberately does NOT create the vector indexes nor await indexes: both are irrelevant to
        the `:Table`/`:Column` upsert and would add index-await latency to the cold-fetch turn.
        The full `apply_schema` stays owned by `load_corpus`. Idempotent.
    """
    async with driver.session(database=database) as session:
        for statement in _CATALOG_GRAPH_CONSTRAINTS:
            await session.run(statement)  # type: ignore[arg-type]


async def _read_catalog_meta(session: Any) -> str | None:
    """The stored `:CatalogMeta` sha, or `None` when the singleton is absent —
    the B1 freshness stamp powering the no-op fast path. *session* is anything
    with `.run` (a live session OR a stub recording calls, for the Layer-1 test)."""
    result = await session.run(_READ_CATALOG_META)
    row = await result.single()
    if row is None:
        return None
    return row["catalog_sha"]


async def _read_corpus_meta(session: Any) -> str | None:
    """The stored `:CorpusMeta` corpus_sha, or `None` when the singleton is absent —
    the governed-corpus B1 freshness stamp powering the no-op fast path. *session* is
    anything with `.run` (a live session OR a recording stub, for the Layer-1 test)."""
    result = await session.run(_READ_CORPUS_META)
    row = await result.single()
    if row is None:
        return None
    return row["corpus_sha"]


def _effective_catalog_sha(catalog_export: dict[str, Any]) -> str:
    """The stamp/guard key for a hydration run — the export's own `catalog_sha`, or a
        deterministic content-hash FALLBACK when it is empty or missing.

        An empty sha would silently break BOTH the skip-guard (`current == ""` never triggers a
        no-op) AND the GC predicate (`coalesce(sha,'') <> ''` matches every node, including
        freshly-stamped ones), so one is never propagated. The same content always yields the same
        stamp, so both guards stay idempotent.
    """
    sha = catalog_export.get("catalog_sha")
    if isinstance(sha, str) and sha:
        return sha
    catalog = catalog_export.get("catalog") or {}
    digest = hashlib.sha1(
        json.dumps(catalog, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    _logger.warning(
        "catalog export lacked a catalog_sha; using derived content hash %s "
        "(the skip-guard + GC key off this stable digest)",
        digest,
    )
    return digest


async def load_catalog_graph(
    driver: AsyncDriver,
    catalog_export: dict[str, Any],
    *,
    database: str = "neo4j",
    ensure_schema: bool = True,
    gc: bool = True,
) -> CatalogGraphReport:
    """Independent, enriched, self-healing hydration of the `:Table`/`:Column` catalog graph
        from the MCP catalog EXPORT dict (`{"catalog_sha", "catalog"}`).

        Catalog-OWNED and separate from `load_corpus`: no embeddings, no model parity. Every node
        upsert stamps the run's `catalog_sha`.

        Two write modes:
          * `gc=True` (the EXPLICIT seed/reconcile path) — after upserting, GC deletes any
            `:Table`/`:Column` whose stamp is stale, logging any GC'd column that still had an
            inbound `:USES`.
          * `gc=False` (the ONLINE self-heal wired in `app.py`) — upsert + meta-stamp ONLY, NEVER
            delete, so two replicas booting on DIFFERENT shas during a rolling deploy converge to
            a current-or-SUPERSET graph instead of GC-deleting each other's freshly-stamped nodes.
            Dropped-column GC is deferred to the explicit maintenance op.

        No-op fast path (BOTH modes): if the stored `:CatalogMeta.catalog_sha` already EQUALS this
        run's sha, returns `skipped=True` without writing.

        Atomicity: the upserts, optional GCs and the meta upsert run in ONE `execute_write`, so no
        concurrent reader ever sees a torn graph.
    """
    export_sha = _effective_catalog_sha(catalog_export)
    catalog = catalog_export["catalog"]

    # B1 guard (read-only, outside the write txn): skip when the graph already
    # carries this run's sha. A truthy stored sha that matches ⇒ no-op.
    async with driver.session(database=database) as session:
        current_sha = await _read_catalog_meta(session)
    if current_sha and current_sha == export_sha:
        _logger.info(
            "catalog graph already at catalog_sha=%s; skipping hydration (B1 no-op)",
            export_sha,
        )
        return CatalogGraphReport(
            catalog_sha=export_sha,
            skipped=True,
            tables_upserted=0,
            columns_upserted=0,
            tables_gc=0,
            columns_gc=0,
            drift_referenced_columns=(),
        )

    if ensure_schema:
        # Constraints-ONLY ensure (Table.key / Column.key / CatalogMeta.id) — no
        # vector-index DDL, no `awaitIndexes` on the cold-fetch turn (H1/L2).
        await apply_catalog_graph_schema(driver, database=database)

    table_rows, column_rows, _table_keys, _column_keys = _catalog_graph_rows(catalog)

    async with driver.session(database=database) as session:

        async def _write(tx: AsyncManagedTransaction) -> tuple[list[dict[str, Any]], int]:
            await tx.run(_UPSERT_TABLES, rows=table_rows, sha=export_sha)
            await tx.run(_UPSERT_COLUMNS, rows=column_rows, sha=export_sha)
            gc_cols: list[dict[str, Any]] = []
            deleted_tables = 0
            if gc:
                gc_cols_result = await tx.run(_GC_COLUMNS, sha=export_sha)
                gc_cols = await gc_cols_result.data()
                gc_tables_result = await tx.run(_GC_TABLES, sha=export_sha)
                gc_tables_row = await gc_tables_result.single()
                deleted_tables = gc_tables_row["deleted"] if gc_tables_row is not None else 0
            await tx.run(_UPSERT_CATALOG_META, sha=export_sha)
            return gc_cols, deleted_tables

        gc_cols, tables_gc = await session.execute_write(_write)

    drift_referenced = _referenced_gc_columns(gc_cols)
    if drift_referenced:
        _logger.warning(
            "catalog GC dropped %d :Column(s) still referenced by a blueprint :USES "
            "(catalog is source of truth — deleted anyway): %s",
            len(drift_referenced),
            list(drift_referenced),
        )

    report = CatalogGraphReport(
        catalog_sha=export_sha,
        skipped=False,
        tables_upserted=len(table_rows),
        columns_upserted=len(column_rows),
        tables_gc=tables_gc,
        columns_gc=len(gc_cols),
        drift_referenced_columns=drift_referenced,
    )
    _logger.info("catalog graph hydration complete: %s", report)
    return report


async def load_corpus(
    driver: AsyncDriver,
    embedding_client: EmbeddingClient,
    blueprints: list[BlueprintSeed],
    knowledge: list[KnowledgeSeed],
    *,
    model_id: str,
    database: str = "neo4j",
    ensure_schema: bool = True,
    catalog: CatalogHandle | None = None,
    corpus_sha: str = "",
    gc: bool = False,
    dimension: int | None = None,
) -> LoadReport:
    """Embed + upsert the seed corpus into neo4j (idempotent). See module docs.

        *dimension* is the vector-index embedding dimension. `None` INFERS it — from the
        just-embedded corpus vectors when present, else a one-shot probe — so the schema is shaped
        to the live embedding model without a code edit; pass an int to pin it.

        Raises `CorpusLoadError` on a malformed `uses` key or a write-time model-parity violation.

        Each seed carries a `source`/`verified` trust stamp (defaulting `mcp`/`True`, so the
        fixture path writes TRUSTED canon; the learning landing writer overrides to
        `learning`/`False`). These flow onto the node so recall's `source='mcp'` trust gate and
        the corpus GC can partition the trusted canon from the learning staging tier.

        *corpus_sha* + *gc* mirror `load_catalog_graph`'s self-healing reconcile, SCOPED to
        `source='mcp'`: every seeded node is stamped; a truthy sha enables the no-op fast path
        (returning `skipped=True` without re-embedding); and `gc=True` (the explicit seed op)
        additionally DELETES any `source='mcp'` node whose stamp is stale. The GC WHERE clause is
        `source='mcp'`-scoped, so it can NEVER touch a learning node. An empty *corpus_sha* never
        skips and never stamps the singleton.

        *catalog*, when supplied, cross-checks every blueprint's `uses` tables and logs a SOFT
        WARNING per blueprint referencing an uncatalogued table. The load ALWAYS proceeds — this
        is a dev-time early warning, never a `CorpusLoadError`, and the real production safety is
        MCP-fails-closed plus both catalogs agreeing.
    """
    # Governed-corpus B1 no-op fast path (Phase 2): when a truthy corpus_sha is
    # supplied AND the `:CorpusMeta` singleton already carries it, the seeded canon is
    # already at this content — skip embed + write entirely (the expensive part is the
    # embed). Checked BEFORE `apply_schema` (mirroring `load_catalog_graph`), so a
    # sha-match cold fetch pays NO DDL + `awaitIndexes` cost either. An empty/absent
    # meta ⇒ proceed. The landing writer + Layer-1 tests pass no corpus_sha, so they
    # never enter this path (byte-identical to pre-Phase-2).
    if corpus_sha:
        async with driver.session(database=database) as session:
            current_sha = await _read_corpus_meta(session)
        if current_sha and current_sha == corpus_sha:
            _logger.info(
                "corpus already at corpus_sha=%s; skipping load (B1 no-op)", corpus_sha
            )
            return LoadReport(
                model_id=model_id,
                blueprints_written=0,
                knowledge_written=0,
                columns_referenced=0,
                tables_referenced=0,
                skipped=True,
            )

    # S2: validate the highest-risk contract BEFORE any embed/write — a malformed
    # scope key fails the whole load loudly rather than silently storing a
    # blueprint the scope filter will always drop. The full-DAG validation (§1.2)
    # runs in the same pre-write pass so an authoring mistake never ships.
    #
    # ORDER (plan §2b): `uses` grammar → reference resolution → full-DAG validation.
    # Resolution INLINES each referenced blueprint's SQL and enforces the `uses` UNION
    # rule, and it must sit between the two: it compares scope keys (so the grammar has
    # to hold first) and it produces the templates gate (c) checks (so it has to run
    # before the DAG validation). Everything after this point — embedding, the
    # structural key, the `composes_json` property, the executor — sees only inline SQL.
    for bp in blueprints:
        validate_blueprint_uses(bp)
    blueprints = resolve_blueprint_references(blueprints)
    for bp in blueprints:
        validate_blueprint_dag(bp)

    # D94 Part 3: SOFT seed-time catalog-skew warning (optional, dev-time only).
    if catalog is not None:
        _warn_on_catalog_skew(blueprints, catalog)

    # Part B / S2: fail a STALE-INDEX dimension mismatch BEFORE the bulk corpus embed.
    # The online B1 self-heal re-arms on failure (CorpusCache), so if we embedded the
    # whole corpus first, a persistent mismatch would pay fetch+full-embed+raise EVERY
    # turn. When an index already EXISTS, resolve the target cheaply (configured, or a
    # SINGLE probe embed) and check parity now. A FRESH graph (no index) can't fail this
    # check — its dimension is inferred from the corpus vectors below (no probe needed).
    resolved_dimension: int | None = None
    if ensure_schema:
        async with driver.session(database=database) as session:
            existing_dims = await fetch_existing_vector_dims(session)
        if existing_dims:
            resolved_dimension = await resolve_embedding_dimension(
                embedding_client, configured=dimension
            )
            check_dimension_parity(existing_dims, resolved_dimension)

    # Embed offline through the SAME endpoint the online path uses (parity by
    # construction). Order-preserving: `embed` returns one vector per input. Done
    # BEFORE apply_schema (Part B) so the vector length can INFER the schema dimension
    # with NO extra probe embed when the corpus is non-empty (fresh-graph case).
    bp_vectors = await embedding_client.embed([b.intent for b in blueprints]) if blueprints else []
    kn_vectors = await embedding_client.embed([k.text for k in knowledge]) if knowledge else []

    # Shape the schema to the resolved dimension. The stale-index case resolved +
    # checked it above (pre-embed); the fresh-graph case infers it from the corpus
    # vectors here (config → sample → probe; the S1 cross-check fires when both a
    # configured value and a sample are present).
    if ensure_schema:
        if resolved_dimension is None:
            resolved_dimension = await resolve_embedding_dimension(
                embedding_client, configured=dimension, sample_vectors=bp_vectors + kn_vectors
            )
        await apply_schema(driver, dimension=resolved_dimension, database=database)

    columns: set[str] = set()
    tables: set[str] = set()
    for bp in blueprints:
        for key in bp.uses:
            columns.add(key)
            tables.add(key.rsplit(".", 1)[0])

    # Aggregated per-blueprint blueprint→column edge drift (blueprint id →
    # sorted missing column keys the catalog graph does not carry). Populated in
    # `_write`, logged once AFTER the txn commits (§ MERGE→MATCH drift signal).
    missing_by_blueprint: dict[str, list[str]] = {}

    # Serialize the DAG properties BEFORE opening the write txn. `dag_properties` now
    # sqlglot-parses each template to derive the `structural_key`, and CPU work inside a
    # write transaction holds neo4j locks for no reason — the computation depends only on
    # the seeds, so it belongs out here with the embedding step.
    dag_props = [dag_properties(bp) for bp in blueprints]

    async with driver.session(database=database) as session:

        async def _write(tx: AsyncManagedTransaction) -> None:
            # S3: the parity read + check are the FIRST statements of the write
            # transaction (not a separate auto-commit read), so two concurrent
            # seeders with different models cannot both pass-then-commit —
            # whichever commits second sees the first's stamp and is refused.
            existing = await fetch_existing_models(tx)
            check_model_parity(existing, model_id)
            for bp, vector, props in zip(blueprints, bp_vectors, dag_props, strict=True):
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
                    created_by=bp.created_by,
                    source_candidate_id=bp.source_candidate_id,
                    source=bp.source,
                    verified=bp.verified,
                    corpus_sha=corpus_sha,
                    **props,
                )
                # S1: unconditional — rewrites the edge set (delete-then-add), so
                # a shrunk uses set leaves no phantom :USES edges. MERGE→MATCH:
                # links only PRE-EXISTING catalog :Column nodes and RETURNs the
                # missing keys (a blueprint referencing an uncatalogued column).
                rewrite = await tx.run(
                    _REWRITE_BLUEPRINT_EDGES, id=bp.id, use_edges=_use_edges(bp.uses)
                )
                row = await rewrite.single()
                missing = row["missing"] if row is not None else []
                if missing:
                    missing_by_blueprint[bp.id] = sorted(missing)
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
                    drift_status=kn.drift_status,
                    created_by=kn.created_by,
                    source_candidate_id=kn.source_candidate_id,
                    source=kn.source,
                    verified=kn.verified,
                    corpus_sha=corpus_sha,
                )
            # Governed-corpus reconcile (Phase 2): on gc=True, DELETE any stale
            # `source='mcp'` node this run did not re-stamp (dropped blueprint/
            # knowledge). The GC Cypher is `source='mcp'`-scoped, so a
            # `source='learning'` staging node can NEVER be reaped here. gc=False
            # (online + landing writer) is additive-only — never deletes.
            if gc:
                bp_gc = await tx.run(_GC_BLUEPRINTS, corpus_sha=corpus_sha)
                bp_gc_row = await bp_gc.single()
                kn_gc = await tx.run(_GC_KNOWLEDGE, corpus_sha=corpus_sha)
                kn_gc_row = await kn_gc.single()
                _logger.info(
                    "corpus GC (source='mcp') removed %d blueprint(s) + %d knowledge "
                    "chunk(s) with a stale corpus_sha",
                    bp_gc_row["deleted"] if bp_gc_row is not None else 0,
                    kn_gc_row["deleted"] if kn_gc_row is not None else 0,
                )
            # Stamp the freshness singleton ONLY for a real (truthy) corpus_sha, and
            # in the SAME txn as the upserts/GC (atomic). The landing writer + Layer-1
            # tests (empty corpus_sha) never touch `:CorpusMeta`, so the online no-op
            # fast path is never corrupted by a learning-tier write.
            if corpus_sha:
                await tx.run(_UPSERT_CORPUS_META, corpus_sha=corpus_sha)

        await session.execute_write(_write)
        # New nodes populate the vector index asynchronously — wait for it so a
        # caller (or the Layer-2 seed fixture) can recall immediately.
        await session.run("CALL db.awaitIndexes(300)")  # type: ignore[arg-type]

    # Blueprint→column drift (MERGE→MATCH): one structured warning if any blueprint
    # `uses` a column key the catalog graph does not carry (its :USES edge was
    # silently skipped). Seed the catalog graph first (`load_catalog_graph`) so the
    # nodes exist; a persistent miss is a genuine blueprint/catalog skew.
    if missing_by_blueprint:
        _logger.warning(
            "blueprint→column edge drift: %d blueprint(s) reference column keys absent "
            "from the catalog graph (:USES edges skipped) — %s",
            len(missing_by_blueprint),
            _format_edge_drift(missing_by_blueprint),
        )

    report = LoadReport(
        model_id=model_id,
        blueprints_written=len(blueprints),
        knowledge_written=len(knowledge),
        columns_referenced=len(columns),
        tables_referenced=len(tables),
    )
    _logger.info("neo4j corpus load complete: %s", report)
    return report


async def fetch_existing_models(runner: Any) -> set[str]:
    """Distinct non-null `embedding_model` stamps on the TRUSTED `source='mcp'` partition;
        learning-tier nodes are excluded. Powers BOTH `load_corpus`'s write-txn parity check and
        the hydrator's model-change detection.

        *runner* is anything with `.run` — a session OR a managed transaction. Empty-string stamps
        are RETAINED, not filtered, so a broken or unstamped mcp row surfaces as a parity conflict
        rather than a silent free pass; the Cypher already excludes true NULLs.
    """
    result = await runner.run(_EXISTING_MODELS)
    row = await result.single()
    if row is None:
        return set()
    return set(row["models"] or [])


__all__ = [
    "DEFAULT_EMBEDDING_DIMENSION",
    "BlueprintSeed",
    "CatalogGraphReport",
    "CorpusLoadError",
    "DimensionMismatchError",
    "KnowledgeSeed",
    "LoadReport",
    "apply_catalog_graph_schema",
    "apply_schema",
    "check_dimension_parity",
    "check_model_parity",
    "claim_rebuild_lock",
    "corpus_content_sha",
    "corpus_seeds_from_export",
    "effective_corpus_sha",
    "fetch_existing_models",
    "fetch_existing_vector_dims",
    "load_catalog_graph",
    "load_corpus",
    "load_seed_fixtures",
    "nuke_graph",
    "rebuild_mcp_corpus_partition",
    "resolve_blueprint_references",
    "resolve_embedding_dimension",
    "schema_statements",
]
