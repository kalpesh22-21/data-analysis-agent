"""corpus_loader — offline write path for the neo4j retrieval corpus (Slice 2).

`load_corpus(...)` is a reusable, idempotent bulk upsert of a small, curated,
TRUSTED seed (neo4j-corpus-design §3): it embeds every blueprint `intent` and
knowledge `text` through the REAL `HttpEmbeddingClient` (the same D71 endpoint
the online path uses — parity by construction), MERGE-by-id upserts the nodes
with the embedding + `embedding_model` stamp + the denormalized `uses` list
property, and links each blueprint's `:USES` edges to the PRE-EXISTING catalog
`:Column` nodes (MERGE→MATCH — the nodes are owned by `load_catalog_graph`, no
longer minted here) in the SAME transaction (the D60 graph shape; unread at recall).

`resolve_blueprint_references(...)` runs FIRST inside `load_corpus`'s pre-write pass
(plan §2b): a `composes` node may name another blueprint by id instead of carrying
its own SQL, and that reference is resolved and INLINED here, at load. The executor is
untouched (it never resolves a reference), no reference id survives onto the node, and
a composite must DECLARE the union of everything it inlines or the load fails closed —
see the "Blueprint references" section for the rules and their rationale.

`load_catalog_graph(...)` is the separate, catalog-OWNED hydration of the enriched,
self-healing `:Table`/`:Column` graph from the MCP catalog EXPORT dict (no embeds,
no model-parity): every node carries its catalog props + a `catalog_sha` stamp, and
GC removes any node a newer catalog run no longer touches (§ catalog-graph).

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

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from sqlglot import exp
from sqlglot.optimizer.qualify_columns import qualify_columns, validate_qualify_columns
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.schema import MappingSchema

from data_agent.runtime.blueprint.models import (
    DEFAULT_NODE_KIND,
    NODE_REF_KEY,
    SCALAR_CONSUME_REF,
    TABLE_CONSUME_REF,
    Blueprint,
    BlueprintParseError,
    SlotSpec,
)
from data_agent.runtime.blueprint.rules import parse_rule
from data_agent.runtime.blueprint.slots import slot_token_names
from data_agent.runtime.blueprint.structural_key import (
    normalize_structural_grain,
    structural_key_from_templates,
    structural_key_recipe,
)
from data_agent.runtime.blueprint.template import (
    SLOT_TOKEN,
    TemplateBindError,
    assert_read_only_select,
    contains_star,
    parse_template,
    referenced_slots,
    validate_optional_pattern,
)
from data_agent.runtime.blueprint.when import WhenClauseError, validate_when
from data_agent.runtime.retrieval.vector_index import READ_CORPUS_META_QUERY

if TYPE_CHECKING:
    from neo4j import AsyncDriver, AsyncManagedTransaction

    from data_agent.runtime.model.embedding_client import EmbeddingClient
    from data_agent.runtime.provenance.catalog_handle import CatalogHandle

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
    # Provenance (S9-activation Slice 2 review S3). `created_by` distinguishes a
    # hand-authored seed (`"seed"`, the default so existing fixtures stay byte-
    # identical) from a blueprint the LEARNING LOOP landed (`"learning"`), and
    # `source_candidate_id` stamps the originating candidate — so incident response
    # can list/remove everything the loop landed. `source_candidate_id=None` sets no
    # neo4j property (neo4j drops a `SET x = null`), so a fixture node is unchanged.
    created_by: str = "seed"
    source_candidate_id: str | None = None
    # Governed-corpus trust partition (Phase 2). `source` places the node in the
    # TRUSTED MCP-canon partition (`"mcp"`) or the learning STAGING tier
    # (`"learning"`); recall serves ONLY `source="mcp"` (the trust gate in
    # `vector_index._BLUEPRINT_RECALL_QUERY`), and corpus GC only ever touches
    # `source="mcp"` nodes. `verified` is the human-approval flag (Phase-3 triage).
    # Defaults are `mcp`/`True` so the FIXTURE/offline seed path + every existing
    # test produces TRUSTED canon by construction (the fixtures carry no
    # source/verified); the learning landing writer OVERRIDES these to
    # `"learning"`/`False` so a landed node stays out of the trusted recall
    # partition until Phase-3 promotes it.
    source: str = "mcp"
    verified: bool = True
    # The LOOSE cross-authoring-path identity (`runtime/blueprint/structural_key.py`).
    # Empty by DEFAULT: the MCP-canon YAMLs carry a `sql_template`/`composes` and no key,
    # so `_dag_properties` DERIVES one from the seed's own templates + grain at write
    # time. The learning landing seed sets it EXPLICITLY (from the same shared helper,
    # over S4's templates) and that explicit value WINS — purely to save a second parse,
    # since both paths run the identical derivation and must agree by construction. A
    # seed whose templates do not normalize lands with NO key at all.
    structural_key: str = ""
    # --- additive full-DAG fields, the runBlueprint brick (OQ-T1, §1.2). All
    # optional-defaulted so existing D87/D88 fixtures still load (no migration).
    # Stored as JSON-string properties on the `:Blueprint` node; unread by recall.
    resolves: dict[str, str] = field(default_factory=dict)
    slots: list[dict[str, Any]] = field(default_factory=list)
    uses_rules: list[Any] = field(default_factory=list)
    sql_template: str | None = None
    composes: list[dict[str, Any]] = field(default_factory=list)
    result_grain: list[str] | dict[str, Any] | None = None
    # J7 — the OPTIONAL window-anchor declaration (`blueprint.models.WINDOW_ANCHORS`:
    # `"data"` | `"calendar"`). Meaningful only for a WINDOWED blueprint; `None` (the
    # default) means the blueprint makes no claim, nothing is stored, and the read path
    # surfaces nothing — so every existing seed and every non-windowed canon YAML is
    # byte-identical to before. Stored as a plain string property (not `*_json`): it is a
    # closed enum, and `_validate_blueprint_dag` rejects anything outside the set at
    # WRITE, so a stored value is always renderable.
    window_anchor: str | None = None


@dataclass(frozen=True)
class KnowledgeSeed:
    """One hand-authored global-knowledge fixture (entity-agnostic)."""

    id: str
    text: str
    doc_id: str
    title: str | None = None
    status: str = "validated"
    # Provenance/drift (UI Slice 2 — mirror the blueprint side). `created_by`
    # distinguishes a hand-authored seed (`"seed"`, the default so existing
    # `knowledge.yaml` fixtures stay byte-identical) from a chunk the LEARNING LOOP
    # landed (`"learning"`); `source_candidate_id` stamps the originating candidate.
    # `drift_status` is threaded for parity + so a retraction can stamp it (knowledge
    # recall does not read it — only blueprints replay/drift). `source_candidate_id=
    # None` sets no neo4j property (neo4j drops a `SET x = null`), so a fixture node
    # is unchanged.
    drift_status: str = "clean"
    created_by: str = "seed"
    source_candidate_id: str | None = None
    # Governed-corpus trust partition (Phase 2) — the knowledge-side mirror of the
    # blueprint fields. `source="mcp"` is the TRUSTED canon partition recall serves;
    # `source="learning"` is the staging tier recall ignores. Defaults `mcp`/`True`
    # keep the fixture path + existing tests trusted-by-construction; the landing
    # writer overrides to `learning`/`False`. See `BlueprintSeed.source`.
    source: str = "mcp"
    verified: bool = True


@dataclass(frozen=True)
class LoadReport:
    """Outcome of a `load_corpus` run — counts + the model the corpus was
    stamped with (for a CLI/exit summary).

    `columns_referenced`/`tables_referenced` count the DISTINCT column/table keys
    the seeded blueprints REFERENCE (their `uses` footprint) — NOT nodes this loader
    writes. Since the MERGE→MATCH rewrite, `:Column`/`:Table` nodes are owned by
    `load_catalog_graph`; `load_corpus` only links `:USES` to pre-existing catalog
    nodes. These counts are a footprint summary for the CLI, nothing more."""

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
    """Outcome of a `load_catalog_graph` run — the enriched `:Table`/`:Column`
    hydration counts + the run's `catalog_sha`.

    `skipped=True` is the B1 no-op fast path (the graph already carries this
    export's sha, so nothing was written). `drift_referenced_columns` lists any
    `:Column` the GC removed that STILL had an inbound `:USES` — a blueprint
    referencing a column the catalog just dropped (GC wins; the drift is logged).
    """

    catalog_sha: str
    skipped: bool
    tables_upserted: int
    columns_upserted: int
    tables_gc: int
    columns_gc: int
    drift_referenced_columns: tuple[str, ...]


class CorpusLoadError(Exception):
    """Raised on a write-time parity violation (mixed embedding models, §3.3)."""


class DimensionMismatchError(CorpusLoadError):
    """Raised when a pre-existing vector index carries a DIFFERENT embedding
    dimension than the one this run wants to write (Part B dimension-parity).

    A `CREATE VECTOR INDEX ... IF NOT EXISTS` silently KEEPS the old (wrong)
    dimension, so the only way to detect a changed embedding model/dimension is to
    introspect `SHOW VECTOR INDEXES` and raise LOUD — this error is the operator's
    signal that the embedding model changed and the graph must be rebuilt — the
    singleton hydrator daemon catches this and nukes + rebuilds at the new dimension."""


# The default embedding dimension (all-mpnet-base-v2 → 768). Used when a caller
# needs a concrete dimension but the runtime cannot/should not infer one (e.g. the
# offline seed scripts + live integration tests wired to the 768 embedding mock).
# The runtime resolver (`resolve_embedding_dimension`) prefers the configured value
# or an inferred one; this constant is the last-resort literal for direct callers.
DEFAULT_EMBEDDING_DIMENSION = 768


# Hard cap on `composes` DAG size (FIX 3). Phase-1 blueprints are single-node or a
# handful of scalar-converging nodes; anything beyond this is an authoring error /
# adversarial input and is rejected LOUD (never traversed into a stack overflow).
_MAX_COMPOSE_NODES = 64

# The two `consumes` grammars (`$3.company_avg` scalar / `$1` table) come from
# `blueprint/models.py` — one definition shared with the executor and the offline S4
# validator. `count($N)` in a `when` expr is loader-local (Slice-C validations).
_COUNT_REF = re.compile(r"count\(\s*\$(\d+)")
# The reserved database a table-consume placeholder lives under in a consumer
# template. The scope-honesty gate treats `scratch.*` sources as SESSION-GATED
# (D69/OQ-4) — not required in `uses` — while the consumer's warehouse columns
# still must be ⊆ uses.
_SCRATCH_DB = "scratch"


def load_seed_fixtures(
    corpus_dir: Path | str,
) -> tuple[list[BlueprintSeed], list[KnowledgeSeed]]:
    """Read `blueprints.yaml` + `knowledge.yaml` under *corpus_dir* into seeds.

    Raises `CorpusLoadError` on a duplicate id (within OR across the two files,
    QA flag 6): MERGE-by-id would silently let a copy-pasted id overwrite in
    place, masking an authoring mistake — fail loudly instead.
    """
    root = Path(corpus_dir)
    blueprints = [BlueprintSeed(**item) for item in _read_yaml_list(root / "blueprints.yaml")]
    knowledge = [KnowledgeSeed(**item) for item in _read_yaml_list(root / "knowledge.yaml")]
    _reject_duplicate_ids([b.id for b in blueprints] + [k.id for k in knowledge])
    return blueprints, knowledge


_BLUEPRINT_SEED_FIELDS = frozenset(f.name for f in fields(BlueprintSeed))
_KNOWLEDGE_SEED_FIELDS = frozenset(f.name for f in fields(KnowledgeSeed))


def _seed_from_entry(entry_id: str, entry: dict[str, Any], *, kind: str) -> Any:
    """Project one MCP-export entry (`{<field>: <value>}`) onto a `BlueprintSeed`/
    `KnowledgeSeed`, WHITELISTING to the dataclass fields.

    The MCP export is a separate repo's canon, so unknown/extra keys are DROPPED
    (never spread blindly into the dataclass constructor, which would `TypeError`) —
    the same defensive whitelist the catalog-graph prop mappers use. The dict key is
    the authoritative id (falls back to the entry's own `id` only if the key is
    somehow absent). `source`/`verified` come through verbatim when the export carries
    them AS THE RIGHT TYPE (the MCP injects `source="mcp"`, `verified=True`); when
    absent OR malformed, the dataclass DEFAULTS (`mcp`/`True`) present the seed as
    trusted canon.

    **Why malformed falls back to the default rather than through to neo4j.** The upsert
    writes `b.source = $source`, and neo4j REMOVES a property set to null — so an export
    entry carrying an explicit `"source": null` produced a SOURCELESS node. That node is
    invisible to recall (its trust gate is bare `= 'mcp'`, fail-closed) but perfectly
    visible to the prior-art read, which deliberately drops that gate. Rather than teach
    every reader to coalesce, the WRITER is made to always stamp: after this whitelist,
    `source` is a non-empty `str` and `verified` is a `bool` on every seed this loader
    builds, so a sourceless/unstamped node can only come from a hand edit or a foreign
    writer — which is exactly what `priorart.models.TIER_UNSOURCED` is for.

    This is NOT a trust escalation. Everything this function projects came from the MCP
    canon export, fetched over the service-key-authenticated route; trust rests on the
    TRANSPORT, and `source` is provenance metadata the export happens to echo back. The
    dataclass already treats "absent" as canon for exactly that reason — this only
    extends the same rule to "present but not a `str`/`bool`", which is otherwise a
    silent property-deleting write."""
    fields_ = _BLUEPRINT_SEED_FIELDS if kind == "blueprint" else _KNOWLEDGE_SEED_FIELDS
    data = {k: v for k, v in entry.items() if k in fields_}
    data["id"] = entry_id or data.get("id")
    _drop_malformed_trust_stamp(data, entry_id=data["id"], kind=kind)
    return BlueprintSeed(**data) if kind == "blueprint" else KnowledgeSeed(**data)


def _drop_malformed_trust_stamp(
    data: dict[str, Any], *, entry_id: Any, kind: str
) -> None:
    """Coerce a malformed `source`/`verified` so the node write always gets a usable
    value (see `_seed_from_entry`). Mutates *data*.

    **The two fields take DIFFERENT fallbacks, and the asymmetry is the point.**

    `source` must be a NON-EMPTY `str` (`""` would write an empty-string property that
    matches neither trust partition — a third state nothing handles). A malformed one is
    DROPPED so the dataclass default (`mcp`) applies. That is not a trust escalation:
    an exporter emitting `"source": null` is indistinguishable in trust terms from one
    omitting the key entirely — which already defaults to `mcp` — and anyone who controls
    that value could simply have written `"mcp"`. Trust rests on the service-key
    authenticated transport, not on a field in the payload.

    `verified` must be a real `bool`, and a malformed one is set to **`False`**, NOT
    dropped. The `source` argument does not transfer: a present `"verified": "false"`
    plausibly MEANT false, and falling back to the dataclass default would silently
    INVERT it to true. `False` is a legal, honest value — "landed but nobody has verified
    it" — it costs nothing today (recall does not read `verified`), and it stays correct
    when `recheck_verified_only` starts reading it. An ABSENT `verified` still defaults
    to `True` via the dataclass; only a malformed one gets the untrusting value.
    """
    source = data.get("source")
    if "source" in data and not (isinstance(source, str) and source.strip()):
        _logger.warning(
            "%s corpus entry %r carries a malformed `source` (%r); defaulting to the "
            "trusted `mcp` stamp. Writing it through would REMOVE the property (neo4j "
            "drops null-valued sets) and leave a sourceless node.",
            kind,
            entry_id,
            source,
        )
        del data["source"]
    verified = data.get("verified")
    if "verified" in data and not isinstance(verified, bool):
        _logger.warning(
            "%s corpus entry %r carries a non-boolean `verified` (%r); stamping FALSE "
            "(unverified). Not the dataclass default: a malformed value may well have "
            "meant false, and defaulting would invert it to true.",
            kind,
            entry_id,
            verified,
        )
        data["verified"] = False


def _seeds_from_entries(raw: dict[str, Any], *, kind: str) -> list[Any]:
    """Build seeds from a `{<id>: <entry>}` map, DEGRADE-not-fail per entry.

    A non-dict entry, a falsy id, or an entry the dataclass ctor rejects (a missing
    required field → `TypeError`, an out-of-range value → `ValueError`) is SKIPPED with
    a warning — never allowed to fail the whole seed. This is load-bearing: the cache
    re-arms + retries the SAME export every turn on a raised seed, so one malformed
    entry from the (separate-repo) MCP would otherwise brick the corpus indefinitely.

    **The scope of that promise is narrower than it reads, and always was.** It covers
    the PROJECTION step only — turning an export entry into a `BlueprintSeed`. It does
    NOT make the load as a whole tolerant: `load_corpus`'s pre-write pass runs
    `_validate_blueprint_uses`, `resolve_blueprint_references` and
    `_validate_blueprint_dag` over the surviving seeds and raises `CorpusLoadError` on
    the first failure, aborting everything. A malformed scope key, an unparseable
    template, a DAG cycle, and (since plan §2b) a dangling blueprint REFERENCE all brick
    the corpus in exactly the way this function's skip exists to prevent. That is
    deliberate — those are authoring errors that must not ship half-applied, and the
    hydrator logs and retries rather than destructively rebuilding — but it means "never
    fatal to the seed" is a claim about THIS function, not about the load. See
    `_reference_graph` for the reference case, which is the one whose blast radius grew:
    a widely-referenced blueprint is now a single point of failure for the whole load."""
    seeds: list[Any] = []
    for entry_id, entry in raw.items():
        if not isinstance(entry, dict):
            _logger.warning("skipping non-dict %s corpus entry %r", kind, entry_id)
            continue
        if not entry_id:
            _logger.warning("skipping %s corpus entry with a falsy id", kind)
            continue
        try:
            seeds.append(_seed_from_entry(str(entry_id), entry, kind=kind))
        except Exception:  # noqa: BLE001 - one bad entry is skipped, never fatal to the seed
            _logger.warning(
                "skipping malformed %s corpus entry %r (bad shape/values); the rest of "
                "the corpus still loads",
                kind,
                entry_id,
                exc_info=True,
            )
    return seeds


def corpus_seeds_from_export(
    export: dict[str, Any],
) -> tuple[list[BlueprintSeed], list[KnowledgeSeed]]:
    """Build the seed lists from a combined corpus export dict (governed-corpus
    Phase 2): `{"blueprints": {<id>: <entry>}, "knowledge": {<id>: <entry>}, ...}`.

    Each entry is the verbatim blueprint/knowledge fields the MCP `/blueprints/export`
    + `/knowledge/export` routes serve (PLUS `source="mcp"`/`verified=True` injected at
    export time). DEGRADE-not-fail per entry (`_seeds_from_entries`): a non-dict /
    falsy-id / malformed entry is skipped with a warning so a single bad entry from the
    separate MCP repo can never brick the whole corpus seed. This is the online/HTTP
    analogue of `load_seed_fixtures`."""
    blueprints = _seeds_from_entries(export.get("blueprints") or {}, kind="blueprint")
    knowledge = _seeds_from_entries(export.get("knowledge") or {}, kind="knowledge")
    return blueprints, knowledge


def effective_corpus_sha(export: dict[str, Any]) -> str:
    """The stamp/guard key for an online corpus hydration — a stable combination of
    the export's `blueprints_sha` + `knowledge_sha`, or a deterministic content hash
    FALLBACK when either is empty/missing (mirrors `_effective_catalog_sha`).

    A change to EITHER corpus flips the combined stamp, so the B1 skip-guard + GC
    re-run. An empty combined stamp would silently break both guards, so a missing sha
    derives a stable SHA-1 over the `{blueprints, knowledge}` content (sorted keys)."""
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
    """A stable content-hash `corpus_sha` for a SEED-LIST reconcile (the seed script,
    which loads fixtures directly rather than an export dict). Deterministic over the
    seeds' full field content (sorted by id), so a re-seed of unchanged fixtures keeps
    the same stamp (idempotent GC no-op) and any edit flips it (GC reaps stale nodes)."""
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
    """The full idempotent schema DDL (constraints + the two native vector indexes),
    with the vector-index dimension parameterized (Part B).

    The dimension is no longer hardcoded to 768: it is resolved from
    `RuntimeSettings.embedding_dimension` (when set) or INFERRED from the live
    embedder (`resolve_embedding_dimension`), so a different embedding model is
    honored without a code edit. Constraints are unchanged. Every statement is
    idempotent (`IF NOT EXISTS`) so the loader's whole write path is re-runnable —
    BUT note a `CREATE VECTOR INDEX ... IF NOT EXISTS` silently keeps the OLD
    dimension of a pre-existing index, which is why `apply_schema` introspects +
    raises `DimensionMismatchError` BEFORE creating (see `check_dimension_parity`)."""
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


def check_dimension_parity(existing_dims: set[int], target_dim: int) -> None:
    """Refuse to (re)deploy the schema when a pre-existing vector index carries a
    DIFFERENT embedding dimension than *target_dim* (Part B, mirrors
    `check_model_parity`).

    Pure function (Layer-1-testable without infra). *existing_dims* is the set of
    `vector.dimensions` read from `SHOW VECTOR INDEXES` for the two corpus indexes;
    an empty set (no index yet) passes trivially (the create runs at *target_dim*).
    Any dimension other than *target_dim* → `DimensionMismatchError`, naming BOTH the
    stored and target dims and instructing the operator to rebuild — because a
    `CREATE ... IF NOT EXISTS` would silently keep the old (wrong) dimension, so an
    embedding-model change is otherwise undetectable and recall would break."""
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


async def _fetch_existing_vector_dims(runner: Any) -> set[int]:
    """The set of `vector.dimensions` configured on the two corpus vector indexes.

    *runner* is anything with `.run` (a live session OR a recording stub). Absent
    indexes / a `SHOW VECTOR INDEXES` that yields nothing ⇒ an empty set (a fresh
    graph — the create then runs at the target dim). A `None`/non-int dimension row is
    skipped defensively."""
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
    """Resolve the vector-index dimension (Part B): the CONFIGURED value when set,
    else INFERRED from an already-embedded *sample_vectors* (no extra network call),
    else a one-shot PROBE embed of a fixed string (`len(vectors[0])`).

    `HttpEmbeddingClient.embed` guarantees a non-empty finite-float vector, so
    `len(vectors[0])` is the true model dimension. An empty/degenerate probe result
    raises `CorpusLoadError` (the schema cannot be shaped without a dimension).

    S1 cross-check: when BOTH a *configured* value AND a non-empty sample vector are
    present, the sample's length MUST equal *configured* — otherwise the operator set
    `EMBEDDING_DIMENSION` to a value the live model does NOT emit, which would build the
    index at one dimension, write vectors of another, and silently EXCLUDE every row
    from the index (cryptic-empty-recall, the exact class this feature exists to kill).
    Raise `CorpusLoadError` naming both numbers rather than ship a broken index."""
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

    `table_key` is everything before the final dot (`"db.table"`), matching the
    scope-key construction `f"{db_table}.{column}"` (context/scope_filter).
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
    `_dag_properties` so an absent structure carries no phantom `{}`/`[]` and a
    re-seed with the value removed clears the stale `*_json` prop (null `+=` removes)."""
    return json.dumps(value) if value else None


def _str_list(value: Any) -> list[str]:
    """Coerce a value into a list of strings (dropping a non-list to `[]`), for the
    array-of-primitive node props (`grain`, `synonyms`). Casing is preserved (D70)."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _table_node_props(db_table: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Project one catalog entry into the enriched `:Table` node props.

    `key` is `db_table` (byte-identical to `_use_edges`' `table_key`). Scalars +
    arrays are stored natively; `temporal`/`primary_key`/`join_keys`/`measures`
    (nested) are JSON-encoded to `*_json`. `grain_verifiable` defaults True when
    absent (parity with `SemanticCatalogHandle._table_grain`). `catalog_sha` is NOT
    included here — the upsert Cypher stamps it via `$sha`."""
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

    `key` is `f"{db_table}.{name}"` — byte-identical to `_use_edges`' `column_key`
    (asserted by a unit test). `name` mirrors the FULL `key` (the node identity) so
    Neo4j Browser/Bloom caption the column by its `db.table.column` key — consistent
    with :Blueprint/:KnowledgeChunk/:Table; the bare short name (casing preserved, D70)
    is retained separately as `short_name`. `values` (a nested map) is JSON-encoded to
    `values_json`; every other listed field is a native scalar/array. Unknown/adversarial
    extra keys (e.g. `client_defined`, `observed_values`, or fixture-mangled keys) are
    simply not read (whitelist)."""
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
    """Build the batched UNWIND rows for the `:Table`/`:Column` upserts from a
    parsed catalog dict (`{db.table: <entry>}`).

    Returns `(table_rows, column_rows, table_keys, column_keys)` where a
    `table_row` is `{key, props}` and a `column_row` is `{key, table_key, props}`.
    A non-dict entry (or a non-dict column def) is tolerated: the entry is skipped /
    the column def falls back to `{}` (so a mangled fixture never crashes the build,
    it just yields sparse props via the whitelist)."""
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


def _validate_blueprint_uses(bp: BlueprintSeed) -> None:
    """Fail-closed with context on a malformed `uses` entry (S2 / §8).

    The design's own "highest-risk contract": a `uses` key that is not a byte-
    exact `"database.table.column"` scope key is silently dropped by the scope
    pre-filter at recall. Guard it at WRITE — every entry must be a `str` with at
    least 3 NON-EMPTY dot-separated parts, else raise with the offending key so
    an authoring mistake fails loudly instead of retrieving nothing.

    The CONTAINER is type-checked before the loop, and that is not cosmetic. The
    seed dataclass declares `uses: list[str]` but enforces nothing at runtime, and
    the MCP export is a separate repo's JSON: `uses: 5` made this `for` raise a bare
    `TypeError` — an un-wrapped third-party exception out of `load_corpus`, the class
    the module docstring forbids because the hydration cache re-arms and retries the
    same poisoned entry every turn. `uses: "db.t.c"` was worse than a crash: it
    ITERATES CHARACTER-WISE, so every reader downstream (`_use_edges`, the union
    rule, `columns`/`tables`) would see 8 one-character "scope keys". Both now fail
    as one clean `CorpusLoadError`.
    """
    if not isinstance(bp.uses, (list, tuple)):
        raise CorpusLoadError(
            f"blueprint {bp.id}: 'uses' must be a list of database.table.column scope "
            f"keys, got {type(bp.uses).__name__}"
        )
    for key in bp.uses:
        if not isinstance(key, str) or len(key.split(".")) < 3 or not all(key.split(".")):
            raise CorpusLoadError(
                f"blueprint {bp.id}: uses entry {key!r} is not a database.table.column scope key"
            )


# --------------------------------------------------------------------------
# Blueprint references (plan §2b) — LOAD-TIME resolution + INLINING
#
# A `composes` node may name ANOTHER blueprint instead of carrying its own SQL:
#
#     - order: 0
#       output: { detail_a: table }
#       ref:
#         blueprint: bp-employee-check-detail-for-period
#         slots:                       # <CHILD slot name>: <THIS blueprint's slot name>
#           employee: employee
#           period:   period_a
#
# `resolve_blueprint_references` replaces that node's `ref` with the referenced
# blueprint's SQL, renamed into this blueprint's slot vocabulary. Everything
# downstream — `_validate_blueprint_dag`, the structural key, the `composes_json`
# property, the executor, `getBlueprint`'s DAG strip — then sees a node that is
# byte-indistinguishable from a hand-written inline one.
#
# WHY LOAD-TIME AND NOT RUNTIME. The corpus is git-versioned YAML re-seeded as a
# unit, so inlining costs nothing in freshness and buys three things outright: the
# executor needs no change (it never resolves a reference), there is no staleness
# window between a child edit and a parent execution, and the model can never learn
# that composition is nameable (requirement 6) because no reference id survives the
# load. Do NOT add a reference lookup to the executor.
#
# WHAT A REFERENCE MAY POINT AT: a blueprint that resolves to exactly ONE SQL
# statement — a leaf (top-level `sql_template`) or a single-node `composes`. A
# reference carries SQL and nothing else, so a multi-node child would have to be
# SPLICED into the parent DAG (order renumbering, `feeds_from`/`consumes` rewiring,
# merging the sink's `when`/`output` with the referencing node's) and every one of
# those merges is a place a gate can be silently dropped. One statement in, one
# statement out; the rule is checkable in a sentence.
# --------------------------------------------------------------------------

# The node key that carries a reference, and its two sub-keys. `ref` is MUTUALLY
# EXCLUSIVE with `sql_template`: a node is one or the other, never both. `NODE_REF_KEY`
# is IMPORTED from `blueprint/models.py`, not re-declared — the parse layer's
# reject-an-unresolved-reference backstop keys off the same constant, and a second copy
# of the string would let the two silently disagree about what a reference even is.
_REF_BLUEPRINT_KEY = "blueprint"
_REF_SLOTS_KEY = "slots"
_REF_KEYS = frozenset({_REF_BLUEPRINT_KEY, _REF_SLOTS_KEY})

# Hard cap on the reference-CHAIN length (A→B→C→…), independent of the per-blueprint
# `_MAX_COMPOSE_NODES` cap. Resolution is iterative and each blueprint resolves once,
# so a deep chain is not a runaway cost — the cap exists because an inlining chain
# deeper than this makes the SQL a node actually runs untraceable from any single
# YAML file, which is an authoring smell in a corpus whose whole point is auditability.
# Canon uses depth 1.
_MAX_REF_DEPTH = 4

# The trust partition a reference may cross: NONE. Inlining COPIES SQL from the child
# into the parent, so an `mcp` composite referencing a `learning` blueprint would
# launder unverified, human-unapproved SQL into the trusted canon partition that
# recall serves (`vector_index._BLUEPRINT_RECALL_QUERY`'s `source='mcp'` gate). Equal
# is the only rule that cannot be argued into an escalation.
#
# The `status`/`drift_status` pair mirrors that same recall gate, because "retracted"
# has no other definition here: a `status='retired'` or `drift_status='suspect'`
# blueprint is exactly one that recall refuses to serve. Inlining its SQL into a live
# composite would resurrect it under another id.
_RECALLABLE_STATUS = "validated"
_SUSPECT_DRIFT = "suspect"


@dataclass(frozen=True)
class _NodeReference:
    """One validated `ref` on one `composes` node, in resolution-ready shape.

    `slot_map` is `{<child slot name>: <parent slot name>}` — keyed by the CHILD
    deliberately. The resolution OPERATION is "rewrite every bind token in the child's
    SQL into the parent's vocabulary", which needs a total function from child token →
    parent token; keying by the child makes that function single-valued by
    construction (a dict cannot repeat a key), whereas keying by the parent would
    admit `{a: dept, b: dept}` — two parents claiming one child slot, an ambiguity with
    no correct resolution.
    """

    index: int  # position in the raw `composes` list (nodes may lack a usable `order`)
    where: str  # human-facing node label for error messages
    target: str
    slot_map: dict[str, str]


def _is_bind_token_name(name: Any) -> bool:
    """True iff `{name}` tokenizes to exactly the slot bind site *name*.

    DERIVED, never mirrored: the candidate is round-tripped through the SAME
    `referenced_slots` tokenizer the templates are read with, so this cannot drift
    from `template.SLOT_TOKEN` the way a copied regex would (`_TABLE_CONSUME_REF`
    existed in three hand-copied versions before that lesson was written down).

    Load-bearing for SAFETY, not tidiness. A slot-map VALUE is substituted into the
    child's SQL as the literal text `{<value>}`. A value that is not exactly a bind
    token — say `"x} OR 1=1 --"` — would emit `{x} OR 1=1 --}`, i.e. attacker-chosen
    raw SQL spliced into a template that is then parsed and executed. This round-trip
    is the boundary that keeps the substitution a RENAME instead of an injection."""
    return isinstance(name, str) and referenced_slots("{" + name + "}") == {name}


def _seed_compose_nodes(bp: BlueprintSeed) -> list[dict[str, Any]]:
    """*bp*'s raw `composes` entries, with the CONTAINER type-checked first.

    `Blueprint.parse` rejects a non-list `composes` too — but that runs later
    (`_validate_blueprint_dag`), and reference resolution has to walk the nodes
    before then. A non-iterable (`composes: 5`) would raise a bare `TypeError` out of
    `load_corpus`; a STRING would iterate CHARACTER-WISE and look like a perfectly
    valid zero-reference DAG, which is the quieter and worse failure. A non-dict
    ENTRY is passed through untouched — it carries no reference, and `Node.parse`
    owns that error message."""
    if bp.composes is None:
        return []
    if not isinstance(bp.composes, (list, tuple)):
        raise CorpusLoadError(
            f"blueprint {bp.id}: 'composes' must be a list, got {type(bp.composes).__name__}"
        )
    return list(bp.composes)


def _seed_slot_specs(bp: BlueprintSeed) -> dict[str, SlotSpec]:
    """*bp*'s declared slots as `{name: SlotSpec}`, parsed EARLY (before
    `_validate_blueprint_dag`) because reference resolution needs each slot's bind
    TOKENS — a `period_range` occupies two (`{n}_start`/`{n}_end`), everything else one.

    Wraps `BlueprintParseError` as `CorpusLoadError` and re-checks the container type
    for the same reason as `_seed_compose_nodes`. Duplicate names are rejected here as
    well as in `Blueprint.parse`: this function builds a dict, and a silent last-wins
    overwrite would make the reference rename pick one of two colliding specs
    arbitrarily."""
    if bp.slots is None:
        return {}
    if not isinstance(bp.slots, (list, tuple)):
        raise CorpusLoadError(
            f"blueprint {bp.id}: 'slots' must be a list, got {type(bp.slots).__name__}"
        )
    specs: dict[str, SlotSpec] = {}
    for raw in bp.slots:
        try:
            spec = SlotSpec.parse(raw)
        except BlueprintParseError as exc:
            raise CorpusLoadError(f"blueprint {bp.id}: malformed slot — {exc}") from exc
        if spec.name in specs:
            raise CorpusLoadError(f"blueprint {bp.id}: duplicate slot name {spec.name!r}")
        specs[spec.name] = spec
    return specs


def _seed_rules_by_bind(bp: BlueprintSeed) -> dict[str, Any]:
    """The `resolve_via` rules *bp* declares, keyed by the `{token}` each binds
    (`earn_codes` → the parsed `ResolvedRule`).

    Keyed by BIND rather than by `id` because the bind name is what a template
    references and therefore what a reference has to reconcile. The rule OBJECT is
    kept, not just the name, so the caller can compare what two same-named rules
    actually probe. `parse_rule` is total — a static/malformed entry yields `None` — so
    the only guard needed is the container type."""
    if bp.uses_rules is None:
        return {}
    if not isinstance(bp.uses_rules, (list, tuple)):
        raise CorpusLoadError(
            f"blueprint {bp.id}: 'uses_rules' must be a list, got "
            f"{type(bp.uses_rules).__name__}"
        )
    rules: dict[str, Any] = {}
    for raw_rule in bp.uses_rules:
        parsed = parse_rule(raw_rule)
        if parsed is not None:
            rules[parsed.binds] = parsed
    return rules


def _node_reference(bp_id: str, index: int, node: Any) -> _NodeReference | None:
    """Validate and project one node's `ref`, or `None` when the node has none.

    Every field is untrusted (a separate repo's YAML, or a hand edit) and every check
    below is derived from what the value is LATER USED FOR, not from its name:

    | value                | downstream use                          | guard              |
    |----------------------|-----------------------------------------|--------------------|
    | `ref`                | `.get()` of two known keys              | must be a dict     |
    | extra `ref` keys     | nothing — silently ignored              | whitelist, reject  |
    | `ref.blueprint`      | key into the `{id: seed}` dict          | non-empty `str`    |
    | `ref.slots`          | `.items()`, key lookups, set algebra    | must be a dict     |
    | `ref.slots` keys     | matched against child slot NAMES        | bind-token shaped  |
    | `ref.slots` values   | emitted into SQL as the text `{value}`  | bind-token shaped  |

    The `ref.blueprint` guard is the unhashable-value case that has bitten this
    codebase repeatedly: `blueprint: [a, b]` reaches `by_id[...]` and raises
    `TypeError: unhashable type: 'list'` — un-wrapped, out of `load_corpus`. The
    `ref.slots` VALUE guard is the injection boundary (see `_is_bind_token_name`).
    Extra keys are rejected rather than ignored because the realistic authoring
    mistake is `slot:` for `slots:`, which would otherwise resolve to "no mappings
    declared" and produce a confusing downstream error about unmapped child slots."""
    if not isinstance(node, dict):
        return None  # `Node.parse` owns this error; it carries no reference
    # MEMBERSHIP, not value. `ref:` with the body deleted is a real and distinguishable
    # authoring state (`{"ref": None}`), and reading it as "no reference" left a
    # template-LESS query node that `_execute_dag` skips as an empty step — from a
    # blueprint the corpus advertises as validated, carrying a literal `"ref": null` in
    # its stored `composes_json` and no `structural_key` at all. The parse-layer backstop
    # had the identical value-vs-presence bug, so neither line caught it.
    if NODE_REF_KEY not in node:
        return None
    raw = node[NODE_REF_KEY]
    order = node.get("order")
    where = f"node {order}" if isinstance(order, int) and not isinstance(order, bool) else (
        f"composes[{index}]"
    )
    if not isinstance(raw, dict):
        raise CorpusLoadError(
            f"blueprint {bp_id}: {where} 'ref' must be an object with a "
            f"{_REF_BLUEPRINT_KEY!r} (and optional {_REF_SLOTS_KEY!r}), got "
            f"{type(raw).__name__}"
            + (" — the key is present with no body" if raw is None else "")
        )
    # `sorted()` over the offending keys needs a TOTAL order, and dict keys are not
    # mutually comparable: YAML resolves bare `on:`/`no:`/`y:` to BOOLEANS, so
    # `{blueprint: …, on: x, note: y}` gives `sorted({True, 'note'})` →
    # `TypeError: '<' not supported between 'str' and 'bool'`, un-wrapped, out of
    # `load_corpus`. ONE unknown key of any type never compares, which is why every
    # single-key test passed. Key on `(type name, repr)` — total for any two objects,
    # deterministic, and it still prints the offending keys the author has to find.
    unknown = sorted(
        (k for k in raw if k not in _REF_KEYS), key=lambda k: (type(k).__name__, repr(k))
    )
    if unknown:
        raise CorpusLoadError(
            f"blueprint {bp_id}: {where} 'ref' has unknown key(s) "
            f"{[repr(k) for k in unknown]} (allowed: {sorted(_REF_KEYS)})"
        )
    target = raw.get(_REF_BLUEPRINT_KEY)
    if not isinstance(target, str) or not target.strip():
        raise CorpusLoadError(
            f"blueprint {bp_id}: {where} 'ref.{_REF_BLUEPRINT_KEY}' must be a non-empty "
            f"blueprint id string, got {target!r}"
        )
    # STRIP, and use the stripped value for the lookup. The emptiness test above already
    # stripped; looking up the raw value meant `" bp-child "` passed as non-empty and
    # then failed as "unknown blueprint", which sends the author hunting for a missing
    # file. Blueprint ids are bare tokens in every authored corpus, so stripping cannot
    # resolve to a DIFFERENT blueprint than the author meant.
    target = target.strip()
    raw_slots = raw.get(_REF_SLOTS_KEY)
    if raw_slots is None:
        raw_slots = {}
    if not isinstance(raw_slots, dict):
        raise CorpusLoadError(
            f"blueprint {bp_id}: {where} 'ref.{_REF_SLOTS_KEY}' must be an object mapping "
            f"the referenced blueprint's slot names to this blueprint's, got "
            f"{type(raw_slots).__name__}"
        )
    slot_map: dict[str, str] = {}
    for child_slot, parent_slot in raw_slots.items():
        if not _is_bind_token_name(child_slot) or not _is_bind_token_name(parent_slot):
            raise CorpusLoadError(
                f"blueprint {bp_id}: {where} 'ref.{_REF_SLOTS_KEY}' entry "
                f"{child_slot!r}: {parent_slot!r} is not a slot-name → slot-name pair "
                "(both sides must be plain slot identifiers — the value is emitted into "
                "SQL as a `{token}`, so anything else would splice raw text into the "
                "referenced template)"
            )
        slot_map[child_slot] = parent_slot
    # `sql_template` and `ref` are mutually exclusive: with both, one is dead and it is
    # not knowable WHICH the author meant to run. `Node.parse` would happily keep the
    # inline one and the reference would evaporate — the silent branch, so reject here.
    if node.get("sql_template") is not None:
        raise CorpusLoadError(
            f"blueprint {bp_id}: {where} declares BOTH a 'sql_template' and a 'ref' — a "
            "node is one or the other (with both, the reference would be silently dropped)."
        )
    # A `consumes` on a reference node is either dead or a hidden coupling. The child
    # cannot know the parent's upstream outputs, so a scalar consume's `{placeholder}`
    # is absent from the inlined SQL (dead — a silently unapplied value), and a TABLE
    # consume would have to match a `scratch.<placeholder>` token INSIDE the child,
    # making the child's internal naming part of the parent's wiring contract. Feeding
    # a referenced blueprint from an upstream node is deliberately out of scope; say so.
    if node.get("consumes"):
        raise CorpusLoadError(
            f"blueprint {bp_id}: {where} declares both 'consumes' and 'ref' — a referenced "
            "blueprint's SQL cannot bind an upstream node's output (it knows nothing about "
            "this DAG). Inline the SQL in this node instead."
        )
    return _NodeReference(index=index, where=where, target=target, slot_map=slot_map)


def _reference_graph(
    by_id: dict[str, BlueprintSeed],
) -> dict[str, list[_NodeReference]]:
    """`{blueprint id: [validated references]}` for every seed that has any.

    A reference to an id absent from THIS load is fatal (requirement 5). "Absent"
    covers deleted, renamed, never-authored, and — because the whole load is one
    fail-closed unit — a child that is itself unloadable for any other reason: if the
    child's own validation raises, no blueprint is written at all, so a composite can
    never ship holding SQL from a blueprint that did not.

    **The availability trade, stated plainly.** `_seeds_from_entries` SKIPS a malformed
    export entry precisely so one bad entry from the separate corpus repo cannot brick
    the corpus; this function then fails the ENTIRE load when a reference points at an
    id that entry would have supplied, and the hydrator deliberately does not catch it
    (it logs and retries every poll rather than destructively rebuilding). So a skipped
    child does, transitively, what the skip exists to prevent — and the canon conversion
    that introduced the first real reference made the referenced blueprint the
    highest-blast-radius entry in the export.

    Considered and DECLINED for this slice: skipping the referencing parent too when its
    target was present-but-skipped, reserving whole-load failure for a genuinely
    never-authored id. It is a coherent asymmetry, but it would make the loader tolerant
    of exactly ONE of the many ways a bad entry aborts the load — an unparseable
    template, a bad scope key, a DAG cycle and a `uses` under-declaration all still abort
    — so it buys a special case rather than a property. It also needs the set of skipped
    ids threaded from `corpus_seeds_from_export` through `load_corpus` into this
    resolver, which is real plumbing on the request path for a case that has never
    occurred. If corpus availability is later made a first-class goal, do it uniformly
    (a per-blueprint quarantine in `load_corpus`), not here."""
    graph: dict[str, list[_NodeReference]] = {}
    for bp in by_id.values():
        refs = [
            ref
            for index, node in enumerate(_seed_compose_nodes(bp))
            if (ref := _node_reference(bp.id, index, node)) is not None
        ]
        if not refs:
            continue
        for ref in refs:
            if ref.target not in by_id:
                raise CorpusLoadError(
                    f"blueprint {bp.id}: {ref.where} references unknown blueprint "
                    f"{ref.target!r} — a reference to a missing or retracted blueprint "
                    "fails the load (the composite would otherwise ship with no SQL)."
                )
        graph[bp.id] = refs
    return graph


def _reference_resolution_order(graph: dict[str, list[_NodeReference]]) -> list[str]:
    """Blueprint ids in CHILD-BEFORE-PARENT order, raising on a cycle.

    Iterative DFS colouring with an explicit stack — the same shape, and for the same
    reason, as `_validate_dag_structure`'s intra-DAG check: a long reference chain must
    fail as a clean `CorpusLoadError`, never as a `RecursionError` escaping
    `load_corpus`. That existing check is scoped to ONE blueprint's `feeds_from` edges
    and structurally cannot see A→B→A; this is its cross-blueprint sibling, and the two
    are independent (a corpus can be free of intra-DAG cycles and still have a
    reference cycle).

    Iteration order follows the seed list, so the reported cycle is deterministic."""
    white, grey, black = 0, 1, 2
    color: dict[str, int] = {}
    order: list[str] = []
    for start in graph:
        if color.get(start, white) != white:
            continue
        stack: list[tuple[str, bool]] = [(start, True)]
        path: list[str] = []
        while stack:
            bp_id, entering = stack.pop()
            if not entering:
                color[bp_id] = black
                order.append(bp_id)
                path.pop()
                continue
            if color.get(bp_id, white) == black:
                continue
            color[bp_id] = grey
            path.append(bp_id)
            stack.append((bp_id, False))
            for ref in graph.get(bp_id, ()):
                state = color.get(ref.target, white)
                if state == grey:
                    raise CorpusLoadError(
                        "blueprint reference cycle spanning blueprints: "
                        f"{' -> '.join([*path, ref.target])}. A reference is INLINED at "
                        "load, so a cycle has no fixed point — it fails the load."
                    )
                if state == white:
                    stack.append((ref.target, True))
    return order


def _assert_reference_depth(
    order: list[str], graph: dict[str, list[_NodeReference]]
) -> None:
    """Reject a reference CHAIN longer than `_MAX_REF_DEPTH`. *order* is child-first,
    so each child's depth is already known when its parent is reached."""
    depth: dict[str, int] = {}
    for bp_id in order:
        refs = graph.get(bp_id, ())
        depth[bp_id] = max((depth[ref.target] + 1 for ref in refs), default=0)
        if depth[bp_id] > _MAX_REF_DEPTH:
            raise CorpusLoadError(
                f"blueprint {bp_id}: reference chain is {depth[bp_id]} deep, exceeding the "
                f"{_MAX_REF_DEPTH}-level cap — the SQL a node actually runs would no longer "
                "be traceable from any single blueprint file."
            )


def _assert_reference_target_loadable(parent: BlueprintSeed, child: BlueprintSeed, where: str) -> None:
    """Refuse to inline from a child in a different trust partition, or from one recall
    would refuse to serve (requirement 5, "retracted").

    Both gates are DERIVED from `vector_index._BLUEPRINT_RECALL_QUERY`, which is the
    only place the corpus defines "servable": `source = 'mcp'` (bare equality),
    `coalesce(status,'validated') = 'validated'`, `coalesce(drift_status,'clean') <>
    'suspect'`. Inlining copies the child's SQL into the parent, so a retracted or
    learning-tier child would keep running under the parent's id — retraction that does
    not retract, and a trust-partition crossing that no reader could see.

    The `coalesce` halves are mirrored EXACTLY, via the property write in between.
    `_UPSERT_BLUEPRINT` does `SET b.status = $status`, and neo4j REMOVES a property set
    to null — so a Python `None` becomes an ABSENT property, which recall coalesces to
    `validated` and serves. The first cut read `None` as `""`, failed the equality, and
    refused the load with a message claiming recall would not serve it; that was false,
    and `status: null` is reachable from the export (`_seed_from_entry` sanitizes
    `source`/`verified`, not `status`), so a servable child would have bricked the whole
    corpus. `""` is a DIFFERENT case and stays refused: an empty string is written as a
    real property, coalesce leaves it alone, and it matches neither partition. A
    non-`str` `status` also stays refused — it is written as some non-string property and
    fails recall's equality just the same. Drift needs no coalesce: the test is
    `== 'suspect'`, which `None` and any non-string already fail."""
    if child.source != parent.source:
        raise CorpusLoadError(
            f"blueprint {parent.id}: {where} references {child.id!r}, which is in the "
            f"{child.source!r} trust partition while this blueprint is in {parent.source!r}. "
            "Inlining copies SQL across that boundary — refused."
        )
    status = _RECALLABLE_STATUS if child.status is None else child.status
    if status != _RECALLABLE_STATUS or child.drift_status == _SUSPECT_DRIFT:
        raise CorpusLoadError(
            f"blueprint {parent.id}: {where} references {child.id!r}, which recall will not "
            f"serve (status={child.status!r}, drift_status={child.drift_status!r}). Inlining "
            "it would keep a retracted blueprint running under this id."
        )


def _referenced_sql_template(parent_id: str, where: str, child: BlueprintSeed) -> str:
    """The ONE SQL statement *child* contributes to a referencing node.

    A leaf (`sql_template`, no `composes`) contributes it directly. A single-node
    `composes` contributes its one node's template — that shape exists so a reference
    CHAIN is possible at all (a leaf carries no nodes and therefore no `ref`), which is
    what makes the depth cap and the cross-blueprint cycle check live rules rather than
    dead code.

    Everything the child's node declares BESIDES the SQL is refused rather than
    dropped, because the referencing node keeps its own. The control-flow trio —
    `node_kind`, `when`, `requires_approval` — are the ones that matter: silently
    discarding a gate is how an approval pause disappears. `feeds_from`/`consumes`
    cannot mean anything in a one-node DAG. `output` IS ignored, deliberately and
    alone: it describes what a node hands to a DOWNSTREAM sibling, a one-node DAG has
    none, and the referencing node declares its own.

    `node_kind` was MISSING from that list for a review cycle, and the docstring above
    it asserted the list was complete — the confident-comment-contradicting-code shape
    this codebase keeps paying for. It is a gate ON ITS OWN, not a modifier of
    `requires_approval`: `executor._execute_dag` pauses on `node.node_kind ==
    "approval" or node.requires_approval`, and the loader's own gate (i) accepts an
    approval node that carries a `sql_template`, so `{order: 0, node_kind: "approval",
    sql_template: ...}` was a legal, silently-de-gated reference target. Anything
    PRESENT and not `"query"` is refused; absent is fine (`"query"` is the default and
    what every canon node means). The check is `!= "query"` rather than `== "approval"`
    so a future `NODE_KINDS` member is refused by default instead of waved through.

    Called only after the child has itself been resolved (child-first order), so its
    node template is already inlined if it was a reference."""
    composes = _seed_compose_nodes(child)
    if child.sql_template is not None and composes:
        raise CorpusLoadError(
            f"blueprint {parent_id}: {where} references {child.id!r}, which declares BOTH a "
            "top-level sql_template and a composes DAG (no execution mode)."
        )
    if child.sql_template is not None:
        if not isinstance(child.sql_template, str) or not child.sql_template.strip():
            raise CorpusLoadError(
                f"blueprint {parent_id}: {where} references {child.id!r}, whose sql_template "
                f"is not usable SQL ({child.sql_template!r})."
            )
        return child.sql_template
    if len(composes) != 1:
        raise CorpusLoadError(
            f"blueprint {parent_id}: {where} references {child.id!r}, which resolves to "
            f"{len(composes)} DAG node(s). A reference inlines exactly ONE SQL statement, "
            "so the target must be a leaf blueprint or a single-node composite."
        )
    node = composes[0]
    if not isinstance(node, dict):
        raise CorpusLoadError(
            f"blueprint {parent_id}: {where} references {child.id!r}, whose single compose "
            "node is not an object."
        )
    node_kind = node.get("node_kind")
    if node_kind is not None and node_kind != DEFAULT_NODE_KIND:
        raise CorpusLoadError(
            f"blueprint {parent_id}: {where} references {child.id!r}, whose node declares "
            f"node_kind={node_kind!r}. A reference takes only SQL, and the referencing node "
            f"carries the default {DEFAULT_NODE_KIND!r} — an approval gate would be silently "
            "dropped."
        )
    for key in ("when", "requires_approval"):
        if node.get(key):
            raise CorpusLoadError(
                f"blueprint {parent_id}: {where} references {child.id!r}, whose node declares "
                f"{key!r}. A reference takes only SQL — that gate would be silently dropped."
            )
    for key in ("feeds_from", "consumes"):
        if node.get(key):
            raise CorpusLoadError(
                f"blueprint {parent_id}: {where} references {child.id!r}, whose single node "
                f"declares {key!r} — it has no upstream node to take it from."
            )
    template = node.get("sql_template")
    if not isinstance(template, str) or not template.strip():
        raise CorpusLoadError(
            f"blueprint {parent_id}: {where} references {child.id!r}, whose single node "
            f"carries no usable sql_template ({template!r})."
        )
    return template


def _reference_token_rename(
    parent: BlueprintSeed,
    child: BlueprintSeed,
    ref: _NodeReference,
    template: str,
) -> dict[str, str]:
    """The TOTAL `{child token}` → `{parent token}` map for one reference.

    Total is the whole point: every bind token the child's template references gets an
    entry (identity for a rule bind), so the substitution below can never leave a token
    behind for a later gate to trip over with a confusing message.

    SLOT-COLLISION SEMANTICS, and why each is what it is:

    * **No implicit identity.** A child slot is bound ONLY through an explicit
      `ref.slots` entry, even when the two names are identical. Slots are resolved ONCE
      per blueprint before the DAG walk (`executor._resolve_all_slots`), so after
      inlining the child's slot DECLARATIONS are gone — type, `binds_to`,
      `enum_values`, `optional_pattern`, all of it — and the PARENT's same-named slot
      governs. Letting that happen implicitly means renaming a parent slot silently
      re-points a child's filter at a different domain. `employee: employee` reads as
      redundant and is exactly the case worth writing down.
    * **Child needs a slot the parent does not supply** → refuse, naming the slots. The
      alternative is a `{token}` with nothing to bind it, i.e. a dropped filter (D56).
    * **Parent maps a slot the child does not have, or does not USE** → refuse. A dead
      mapping is an author believing a filter is applied when it is not — the same
      wrong-answer class, arriving from the other direction.
    * **Bind ARITY must match** → refuse on mismatch. A `period_range` occupies two
      tokens and everything else one; mapping a range onto a scalar would emit
      `{p_start}`/`{p_end}` against a parent slot that binds neither.
    * **Bind TYPE and `binds_to` may differ** → WARN, do not refuse. The parent is the
      authority on its own slots (the child's spec is discarded either way) and the
      value still binds as a typed AST literal, so a divergence is a resolution-strictness
      difference, not a safety one. It is worth a log line because the usual cause is a
      copy-paste that will validate values against the wrong domain.
    * **A `resolve_via` rule bind is NOT renamed**, and the parent must re-declare the
      rule itself — see the residual-token branch at the bottom, which also warns when
      the two same-named rules probe different things (the rule-side twin of the
      `binds_to` divergence, and the more consequential one: a rule fires a probe).
    """
    parent_slots = _seed_slot_specs(parent)
    child_slots = _seed_slot_specs(child)
    referenced = referenced_slots(template)

    rename: dict[str, str] = {}
    for child_name, parent_name in sorted(ref.slot_map.items()):
        child_spec = child_slots.get(child_name)
        if child_spec is None:
            raise CorpusLoadError(
                f"blueprint {parent.id}: {ref.where} maps slot {child_name!r}, which "
                f"{child.id!r} does not declare (its slots are {sorted(child_slots)})."
            )
        parent_spec = parent_slots.get(parent_name)
        if parent_spec is None:
            raise CorpusLoadError(
                f"blueprint {parent.id}: {ref.where} feeds {child.id!r}'s slot "
                f"{child_name!r} from {parent_name!r}, which this blueprint does not "
                f"declare (its slots are {sorted(parent_slots)})."
            )
        child_tokens = slot_token_names(child_spec)
        parent_tokens = slot_token_names(parent_spec)
        if len(child_tokens) != len(parent_tokens):
            raise CorpusLoadError(
                f"blueprint {parent.id}: {ref.where} maps {child.id!r} slot {child_name!r} "
                f"(type {child_spec.type!r}, {len(child_tokens)} bind token(s)) onto "
                f"{parent_name!r} (type {parent_spec.type!r}, {len(parent_tokens)}). A "
                "period_range occupies two tokens and every other type one — the arities "
                "must match or the inlined SQL would reference a token nothing binds."
            )
        if not child_tokens & referenced:
            raise CorpusLoadError(
                f"blueprint {parent.id}: {ref.where} maps {child.id!r} slot {child_name!r}, "
                "which its SQL never references — the value would be silently discarded "
                "(a filter the author believes is applied and is not)."
            )
        if child_spec.type != parent_spec.type or child_spec.binds_to != parent_spec.binds_to:
            _logger.warning(
                "blueprint %s: %s feeds %s's slot %r (type=%r binds_to=%r) from %r "
                "(type=%r binds_to=%r); after inlining ONLY this blueprint's declaration "
                "governs, so the value is validated against ITS domain",
                parent.id,
                ref.where,
                child.id,
                child_name,
                child_spec.type,
                child_spec.binds_to,
                parent_name,
                parent_spec.type,
                parent_spec.binds_to,
            )
        # Token-level, suffix-preserving: `{n}`→`{m}` for a scalar slot, and
        # `{n}_start`/`{n}_end`→`{m}_start`/`{m}_end` for a period_range. Built from
        # `slot_token_names` on BOTH sides rather than by string surgery, so a new
        # multi-token slot type is a compile-time-visible change here, not a silent one.
        mapped_here: set[str] = set()
        for suffix in ("_start", "_end", ""):
            child_token = f"{child_name}{suffix}"
            parent_token = f"{parent_name}{suffix}"
            if child_token in child_tokens and parent_token in parent_tokens:
                rename[child_token] = parent_token
                mapped_here.add(child_token)
        if mapped_here != child_tokens:
            raise CorpusLoadError(
                f"blueprint {parent.id}: {ref.where} could not map every bind token of "
                f"{child.id!r} slot {child_name!r} onto {parent_name!r} "
                f"({sorted(child_tokens)} → {sorted(parent_tokens)})."
            )

    unmapped = sorted(referenced - set(rename))
    if unmapped:
        # A residual token is legitimate ONLY if it is a `resolve_via` rule bind — those
        # are resolved blueprint-wide, by NAME, from `uses_rules`, so they are not
        # renamed. The parent must therefore declare the same rule itself. That is
        # authored, not inherited, for the same reason `uses` is: a rule fires a
        # warehouse probe, and a composite must state every probe it causes.
        child_rules = _seed_rules_by_bind(child)
        parent_rules = _seed_rules_by_bind(parent)
        for token in unmapped:
            child_rule = child_rules.get(token)
            if child_rule is None:
                raise CorpusLoadError(
                    f"blueprint {parent.id}: {ref.where} references {child.id!r}, whose SQL "
                    f"binds {{{token}}} — neither one of its slots (map it with "
                    f"'ref.{_REF_SLOTS_KEY}') nor one of its resolve_via rules."
                )
            parent_rule = parent_rules.get(token)
            if parent_rule is None:
                raise CorpusLoadError(
                    f"blueprint {parent.id}: {ref.where} references {child.id!r}, whose SQL "
                    f"binds the resolve_via rule name {{{token}}}. Rules are resolved per "
                    "BLUEPRINT, so this blueprint must declare a matching rule in its own "
                    "uses_rules — it is not inherited."
                )
            # Matching by BIND NAME alone is not the same as matching the rule. The
            # parent's declaration is the one that runs (rules resolve per blueprint), so
            # a same-named rule probing a different column or concept silently feeds the
            # child's `IN {token}` a different value set. This is the rule-side twin of
            # the slot `type`/`binds_to` divergence warned about above, and it is the more
            # consequential of the two: a rule fires a real warehouse probe. Warned, not
            # refused, for the same reason — the parent is the authority, and its probed
            # column is already forced ⊆ its own `uses` by gate (g).
            if (child_rule.table, child_rule.column, child_rule.concept) != (
                parent_rule.table,
                parent_rule.column,
                parent_rule.concept,
            ):
                _logger.warning(
                    "blueprint %s: %s references %s, whose SQL binds {%s} from rule %r "
                    "(%s.%s / concept %r); THIS blueprint's same-named rule %r probes "
                    "%s.%s / concept %r and is the one that will run — the inlined filter "
                    "gets a different value set than the referenced blueprint does",
                    parent.id,
                    ref.where,
                    child.id,
                    token,
                    child_rule.rule_id,
                    child_rule.table,
                    child_rule.column,
                    child_rule.concept,
                    parent_rule.rule_id,
                    parent_rule.table,
                    parent_rule.column,
                    parent_rule.concept,
                )
            rename[token] = token
    return rename


def _inline_reference(
    parent: BlueprintSeed,
    child: BlueprintSeed,
    ref: _NodeReference,
    node: dict[str, Any],
) -> dict[str, Any]:
    """A NEW node dict with `ref` replaced by the child's SQL, renamed into *parent*'s
    slot vocabulary. Never mutates the input node (the caller's seeds are shared)."""
    _assert_reference_target_loadable(parent, child, ref.where)
    template = _referenced_sql_template(parent.id, ref.where, child)
    # A scratch source inside a referenced template is refused. It cannot be satisfied —
    # a reference node may not declare `consumes` (the only thing that materializes a
    # scratch table) — and the child is unrunnable standalone with one, so this is an
    # authoring error in the child that would otherwise surface as a confusing
    # scope-check pass (`_assert_source_tables_in_uses` skips `scratch.*`).
    #
    # Recognized CASE-INSENSITIVELY (`_scratch_db_sources`, NOT
    # `_scratch_placeholder_names`). The exact-match version let `FROM SCRATCH.borrowed`
    # read as an ordinary warehouse table and this gate never fired; the question here is
    # "does this touch session scratch at all", whose authority — the MCP's
    # `_references_scratch_db` — case-folds precisely so a spelling cannot route around
    # the session gate.
    scratch = sorted({f"{db}.{name}" for db, name in _scratch_db_sources(template)})
    if scratch:
        raise CorpusLoadError(
            f"blueprint {parent.id}: {ref.where} references {child.id!r}, whose SQL reads "
            f"session scratch source(s) {scratch}. A referenced blueprint must be runnable "
            "on its own; nothing in this DAG can materialize them for it."
        )
    rename = _reference_token_rename(parent, child, ref, template)
    # SIMULTANEOUS substitution in ONE pass. Sequential per-token replacement would
    # chain (`a`→`b` then `b`→`c` renames the original `a` twice), and swapping two
    # slot names is a realistic mapping.
    inlined = SLOT_TOKEN.sub(lambda m: "{" + rename.get(m.group(1), m.group(1)) + "}", template)
    resolved = {k: v for k, v in node.items() if k != NODE_REF_KEY}
    resolved["sql_template"] = inlined
    return resolved


def _assert_uses_union(
    parent: BlueprintSeed,
    refs: list[_NodeReference],
    footprint: dict[str, frozenset[str]],
) -> None:
    """THE SECURITY GATE (requirement 4). A composite must DECLARE at least the union
    of its referenced blueprints' footprints, or the load fails. Not a warning.

    `uses` is hand-AUTHORED, never derived from the SQL, and it is the corpus's only
    machine-readable statement of what a blueprint reads. Three readers act on it:
    the recall scope pre-filter drops a blueprint whose `uses` is not a subset of the
    caller's `column_scope`; `promotion/token_minter.mint(column_scope=<uses>)` mints
    the golden-replay JWT from it verbatim; and `_validate_blueprint_dag` gate (c)
    checks every template against it. A composite that under-declares is offered to
    users whose scope does not cover what it actually reads — the pre-filter's whole
    job, silently defeated.

    Gate (c) DOES independently re-check the inlined SQL against the parent's `uses`,
    so this is not the only thing standing between a reference and a scope escape.
    The union rule is stricter on purpose: it binds the parent to the child's DECLARED
    footprint rather than to whatever columns the child's SQL happens to name today, so
    a later widening of the child cannot quietly widen every composite that inlines it.
    A child column added upstream fails the parent's load until a human re-declares it.

    TRANSITIVITY. *footprint* accumulates `declared ∪ ⋃ children` in child-first order,
    so a grandchild's columns reach the grandparent even though only direct children are
    inspected. Once this check passes, `footprint[id] == set(declared)` — the union is
    tracked separately anyway so transitivity does not rest on that induction holding.
    """
    declared = _declared_uses(parent)
    required: frozenset[str] = frozenset()
    contributors: dict[str, list[str]] = {}
    for ref in refs:
        child_footprint = footprint.get(ref.target, frozenset())
        for key in sorted(child_footprint - declared):
            contributors.setdefault(key, []).append(ref.target)
        required |= child_footprint
    missing = sorted(required - declared)
    if missing:
        detail = "; ".join(f"{key} (from {sorted(set(contributors[key]))})" for key in missing)
        raise CorpusLoadError(
            f"blueprint {parent.id}: declared `uses` is MISSING {len(missing)} scope key(s) "
            f"its referenced blueprints read — {detail}. A composite's footprint is the "
            "union of everything it inlines; declaring less would let it read columns "
            "outside its advertised scope. Add them to `uses` (fail-closed, by design)."
        )
    footprint[parent.id] = declared | required


def resolve_blueprint_references(blueprints: list[BlueprintSeed]) -> list[BlueprintSeed]:
    """Resolve every `composes` node `ref` by INLINING the referenced blueprint's SQL.

    Pure and hermetic (no I/O, no driver, no embedder) — `load_corpus` calls it as the
    first step of its pre-write pass, so every write path (fixture seed, MCP export
    hydration, the learning landing writer) goes through exactly this. Input seeds are
    never mutated; a blueprint with no references is returned as-is, by identity.

    Order of operations, and why:

      1. build + shape-validate the reference graph (a dangling target fails here);
      2. topologically order it CHILD-FIRST, failing on a cross-blueprint cycle;
      3. cap the chain depth;
      4. resolve in that order, so a child is already inlined when its parent reads it,
         and check the `uses` union per parent against the accumulated footprint.

    Returns the seeds in the ORIGINAL input order — `load_corpus` zips the returned list
    against its embedding vectors, and a reordered list would silently mis-pair them.

    Raises only `CorpusLoadError`. That is a hard requirement, not a style preference:
    this runs inside `load_corpus`, which the hydrator's self-heal poll re-arms and
    retries every turn, so an un-wrapped exception here bricks the corpus indefinitely.
    """
    by_id: dict[str, BlueprintSeed] = {}
    for bp in blueprints:
        # `id` is the KEY of every structure below (this map, the reference graph, the
        # DFS colouring, the footprint accumulator), so it must be a hashable `str`
        # before any of them touch it. The dataclass declares `id: str` and enforces
        # nothing: `load_seed_fixtures` spreads a YAML entry straight into the ctor, so
        # `id: [a, b]` reaches `bp.id in by_id` and raises `TypeError: unhashable type:
        # 'list'` — un-wrapped, out of `load_corpus`, retried every poll. Found by
        # sweeping this path for comparisons/memberships on untrusted values rather than
        # by being pointed at it; the export path happens to be safe (`_seed_from_entry`
        # coerces the dict key with `str()`), the fixture path is not.
        if not isinstance(bp.id, str) or not bp.id:
            raise CorpusLoadError(
                f"blueprint id {bp.id!r} is not a non-empty string "
                f"({type(bp.id).__name__}) — ids key the corpus and every reference to it"
            )
        if bp.id in by_id:
            raise CorpusLoadError(
                f"duplicate blueprint id {bp.id!r} in one corpus load — a reference to it "
                "would resolve to whichever copy happened to be last."
            )
        by_id[bp.id] = bp

    graph = _reference_graph(by_id)
    if not graph:
        return list(blueprints)

    order = _reference_resolution_order(graph)
    _assert_reference_depth(order, graph)

    # `footprint` seeds from every seed's own declared `uses` — for a blueprint that
    # inlines nothing that IS its footprint; `_assert_uses_union` widens the parents
    # as it goes. `_declared_uses` re-runs the scope-key grammar check so this function
    # is fail-closed when called directly (tests, tooling) and not only via
    # `load_corpus`, whose pre-write pass has already validated them.
    footprint: dict[str, frozenset[str]] = {bp.id: _declared_uses(bp) for bp in blueprints}
    for bp_id in order:
        refs = graph.get(bp_id)
        if not refs:
            continue
        parent = by_id[bp_id]
        nodes = _seed_compose_nodes(parent)
        for ref in refs:
            nodes[ref.index] = _inline_reference(
                parent, by_id[ref.target], ref, nodes[ref.index]
            )
        _assert_uses_union(parent, refs, footprint)
        by_id[bp_id] = replace(parent, composes=nodes)
        _logger.info(
            "blueprint %s: inlined %d blueprint reference(s) at load (%s)",
            bp_id,
            len(refs),
            ", ".join(sorted({ref.target for ref in refs})),
        )
    return [by_id[bp.id] for bp in blueprints]


def _declared_uses(bp: BlueprintSeed) -> frozenset[str]:
    """*bp*'s declared scope keys as a set, grammar-checked first.

    The check is not redundant with `load_corpus`'s pre-write pass: this is the set the
    `uses` UNION rule compares, and building it from an unvalidated `uses` is how a
    string silently becomes 8 one-character "scope keys" (see `_validate_blueprint_uses`)."""
    _validate_blueprint_uses(bp)
    return frozenset(bp.uses)


def _warn_on_catalog_skew(blueprints: list[BlueprintSeed], catalog: CatalogHandle) -> None:
    """D94 Part 3 — log a SOFT WARNING per blueprint whose `uses` references a
    `db.table` absent from *catalog*. Never raises: a blueprint may legitimately
    reference tables absent from a partial/dev catalog snapshot, so this is a
    dev-time early warning for the catalog/extractor skew, not a load precondition.
    `uses` keys are `database.table.column` scope keys (grammar enforced by
    `_validate_blueprint_uses`: >=3 non-empty dot-separated parts). The `db.table`
    grouping is derived as everything-before-the-final-dot — the SAME convention as
    `_use_edges`' `table_key` (and the scope-key construction in
    `context/scope_filter`) — so the two parsers agree even for keys with a dotted
    table segment. `is_catalogued(database, table)` reconstructs `f"{database}.{table}"`,
    so splitting that grouping on its FIRST dot round-trips to the same `db.table`.
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


def _compose_node_templates(composes: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """The `(order, sql_template)` pairs of a composite seed's DAG nodes — the shape
    the shared canonicalizer joins in ascending order (§11.2 composite rule).

    A node with no `sql_template` (canon authors output-only DAG nodes) or a non-integer
    `order` is SKIPPED, mirroring the learning side where every `NodeTemplate` carries a
    real template — so both paths join the same set of normalized strings."""
    pairs: list[tuple[int, str]] = []
    for node in composes:
        if not isinstance(node, dict):
            continue
        template = node.get("sql_template")
        order = node.get("order")
        if not isinstance(template, str) or not template.strip():
            continue
        if not isinstance(order, int) or isinstance(order, bool):
            continue
        pairs.append((order, template))
    return pairs


def _seed_structural_key(bp: BlueprintSeed) -> str:
    """The seed's LOOSE cross-tier `structural_key`, or `""` when one cannot be minted.

    An EXPLICIT `bp.structural_key` wins; absent one, DERIVE it from the seed's own
    `sql_template`/`composes` + `result_grain`. Both branches run the SAME
    `structural_key_from_templates` helper, so they agree by construction — the learning
    landing seed stamps its key up front only to save a second sqlglot parse, and the
    MCP-canon tier (whose YAMLs carry no key) always takes the derive branch.

    FAIL-SOFT (D52): an unparseable template yields `""` and a WARNING, never a raise —
    but that branch is defensive DEPTH, not the active load-path behavior. `load_corpus`
    never reaches it with a bad template: `_validate_blueprint_dag` runs unconditionally
    in the earlier pre-write pass and raises `CorpusLoadError` for anything this recipe
    would also reject, aborting the WHOLE load fail-CLOSED (pre-existing by design — an
    authoring mistake must not ship). The guard here becomes live only if the key recipe
    ever grows stricter than loader validation, which
    `test_every_template_the_key_recipe_rejects_is_also_rejected_by_loader_validation`
    watches for."""
    if bp.structural_key:
        return bp.structural_key
    if not bp.sql_template and not bp.composes:
        return ""
    key = structural_key_from_templates(
        bp.result_grain, bp.sql_template, _compose_node_templates(bp.composes)
    )
    if not key:
        # Name the ACTUAL cause. There are two, and they lead an operator to opposite
        # places: an unparseable template, or a `result_grain` carrying a non-string
        # member (a YAML null from a dangling `-`, an unquoted number). Blaming the
        # template for a grain failure sends them to debug SQL that parses fine.
        cause = (
            "result_grain has a non-string member, so the grain is unusable"
            if normalize_structural_grain(bp.result_grain) is None
            else "the template did not normalize"
        )
        _logger.warning(
            "blueprint %s: could not derive a structural_key (%s); the node lands WITHOUT "
            "one and is invisible to cross-tier prior-art matching",
            bp.id,
            cause,
        )
    return key


def _dag_properties(bp: BlueprintSeed) -> dict[str, Any]:
    """Serialize the additive full-DAG fields into the neo4j string properties
    (§1.1). Empty structures are stored as `null` so a DAG-less blueprint carries
    no phantom `{}`/`[]` — additive and back-compatible with D87/D88 seeds.

    `structural_key` follows the same `null`-when-absent rule, and that is
    SAFETY-RELEVANT rather than cosmetic: an empty-string key stored on every
    unparseable blueprint would make a naive `MATCH (b {structural_key: $k})` lookup
    match them ALL as false prior art. Absent means absent.

    `structural_key_recipe` is written ONLY alongside a real key, under the same rule —
    a recipe stamp on a keyless node describes nothing. Unread today; it exists so that a
    sqlglot bump splitting the re-derived canon tier from the write-once learning tier is
    DETECTABLE rather than silent (see `structural_key_recipe`)."""
    key = _seed_structural_key(bp) or None
    return {
        "resolves_json": json.dumps(bp.resolves) if bp.resolves else None,
        "slots_json": json.dumps(bp.slots) if bp.slots else None,
        "uses_rules_json": json.dumps(bp.uses_rules) if bp.uses_rules else None,
        "sql_template": bp.sql_template,
        "composes_json": json.dumps(bp.composes) if bp.composes else None,
        "result_grain_json": (json.dumps(bp.result_grain) if bp.result_grain is not None else None),
        # J7: `or None` so an undeclared anchor writes `null` — neo4j REMOVES a
        # null-valued SET, which is what makes a re-seed that DROPS the declaration
        # actually clear the stale property instead of leaving the old claim behind. The
        # same rule `structural_key` follows two lines down, for the same reason.
        "window_anchor": bp.window_anchor or None,
        "structural_key": key,
        "structural_key_recipe": structural_key_recipe() if key else None,
    }


def _uses_schema(uses: list[str]) -> dict[str, dict[str, dict[str, str]]]:
    """Build a sqlglot `{db: {table: {column: type}}}` schema from the declared
    `uses` scope keys — the ALLOWLIST the template's tables + columns must resolve
    against. A `db.table.column` key groups as db=all-but-last-two,
    table=second-to-last, column=last (matching `f"{db_table}.{column}"`)."""
    schema: dict[str, dict[str, dict[str, str]]] = {}
    for key in uses:
        if not isinstance(key, str):
            continue
        parts = key.split(".")
        if len(parts) < 3 or not all(parts):
            continue
        db = ".".join(parts[:-2])
        table = parts[-2]
        column = parts[-1]
        schema.setdefault(db, {}).setdefault(table, {})[column] = "TEXT"
    return schema


def _scratch_placeholder_names(sql_template: str | None) -> set[str]:
    """The set of `scratch.<placeholder>` table names a template references in a
    FROM/JOIN position (the table-consume bind sites, §2.3). Parsed via
    `parse_template` so a `{slot}` template parses too. A non-parsing template →
    empty set (the other load checks surface the parse failure).

    CASE-SENSITIVE, and that is the correct polarity HERE even though the sibling
    recognizer below is not. This set answers "which placeholders will the executor
    REWRITE", and `template._rewrite_scratch_tables` matches `db == 'scratch'`
    exactly — so gate (h) ("a table-consume must appear as a `scratch.<ph>` source")
    must use the same exact test or it would accept a spelling the executor cannot
    rewrite. Case-folding here would LOOSEN gate (h) while tightening every other
    caller: one function, two callers, opposite fail-closed directions. The two
    questions are therefore split, and `_assert_canonical_scratch_spelling` keeps them
    from ever disagreeing about a template that actually loads."""
    if not sql_template:
        return set()
    try:
        tree = parse_template(sql_template)
    except TemplateBindError:
        return set()
    names: set[str] = set()
    for table in tree.find_all(exp.Table):
        if table.text("db") == _SCRATCH_DB and table.name:
            names.add(table.name)
    return names


def _scratch_db_sources_in_tree(tree: exp.Expression) -> list[tuple[str, str]]:
    """Every `(db_as_written, table)` source in a PARSED template whose database is the
    scratch DB under a CASE-INSENSITIVE match.

    The single tree-level definition of "this source is session scratch", shared by its
    two callers so they cannot drift: `_scratch_db_sources` (which parses a template
    string first) and `_assert_canonical_scratch_spelling` (which already holds the
    tree). Their agreement IS the load-bearing property of the D3 scratch split — the
    canonical-spelling gate is only sound if it recognizes exactly the sources the
    reference gate does — so it gets one implementation, not two identical ones."""
    return [
        (table.text("db"), table.name)
        for table in tree.find_all(exp.Table)
        if table.text("db").casefold() == _SCRATCH_DB and table.name
    ]


def _scratch_db_sources(sql_template: str | None) -> list[tuple[str, str]]:
    """Every `(db_as_written, table)` source whose database is the scratch DB under a
    CASE-INSENSITIVE match — the "does this template touch session scratch at all?"
    question, deliberately over-approximating.

    The authority for that question is the MCP's `service._references_scratch_db`,
    which matches the scratch database name with `re.IGNORECASE` *specifically* so a
    spelling cannot route a query around the session gate, and which documents itself as
    a fail-closed over-approximation. This mirrors that polarity; the exact-match
    sibling above answers a different question (see its docstring).

    Returns the db text AS WRITTEN so a caller can name the offending spelling."""
    if not sql_template:
        return []
    try:
        tree = parse_template(sql_template)
    except TemplateBindError:
        return []
    return _scratch_db_sources_in_tree(tree)


def _assert_canonical_scratch_spelling(bp_id: str, where: str, tree: exp.Expression) -> None:
    """Reject a scratch-database source spelled anything other than `scratch`.

    This is what keeps the two recognizers above from disagreeing on anything that
    actually loads: after this gate, "recognized case-insensitively" and "recognized
    exactly" describe the same set, so gate (h), the executor's rewrite and the
    reference gate cannot diverge.

    Without it the escape is real and not merely cosmetic. sqlglot does NOT normalize
    identifier case on this path (measured: `qualify_tables` and `qualify_columns` both
    leave `SCRATCH` as written), so `FROM SCRATCH.borrowed` reads as an ordinary
    warehouse source; declare `SCRATCH.borrowed.<col>` in `uses` — which passes the
    `db.table.column` grammar unchanged — and `_assert_source_tables_in_uses` finds it
    declared and the whole corpus loads. The result is a `validated` blueprint reading a
    session-scoped table nothing can materialize for it, and an offline golden-replay
    JWT minted from a `column_scope` containing a scratch key. The runtime still fails
    closed (the MCP's own IGNORECASE gate catches it), so this is defence in depth —
    but a blueprint that cannot run should not load."""
    for db, name in _scratch_db_sources_in_tree(tree):
        if db != _SCRATCH_DB:
            raise CorpusLoadError(
                f"blueprint {bp_id}: {where} reads {db}.{name} — the session scratch "
                f"database must be spelled exactly {_SCRATCH_DB!r}. Every reader agrees "
                "the source IS scratch, but only the canonical spelling is rewritten to "
                "the materialized table, so this would load and could never run."
            )


def _template_output_columns(sql_template: str | None) -> list[str]:
    """The producing node's output column names (its SELECT-list aliases) — the
    columns its materialized scratch table will carry, used to register the scratch
    placeholder in the consumer's qualify schema."""
    if not sql_template:
        return []
    try:
        tree = parse_template(sql_template)
    except TemplateBindError:
        return []
    return list(getattr(tree, "named_selects", []) or [])


def _scratch_schema_for_node(
    node: Any, outputs_by_order: dict[int, Any]
) -> dict[str, dict[str, str]]:
    """Build `{placeholder: {column: TEXT}}` for a consumer node's TABLE consumes,
    sourced from each producing node's declared output columns. Lets qualify_columns
    resolve `scratch.<placeholder>` columns while keeping warehouse columns checked
    against `uses` (the D69/OQ-4 scope-honesty split, §2.3)."""
    schema: dict[str, dict[str, str]] = {}
    for placeholder, ref in node.consumes.items():
        match = TABLE_CONSUME_REF.match(str(ref))
        if match is None:
            continue
        src = outputs_by_order.get(int(match.group(1)))
        cols = _template_output_columns(getattr(src, "sql_template", None)) if src else []
        schema[placeholder] = {col: "TEXT" for col in cols}
    return schema


def _assert_source_tables_in_uses(
    bp_id: str,
    where: str,
    qualified: exp.Expression,
    schema_dict: dict[str, dict[str, dict[str, str]]],
) -> None:
    """Assert every SOURCE table (FROM/JOIN) resolves to a `(db, table)` present in
    the uses-schema (review re-review BLOCKER — the qualified-column JOIN hole).

    The column-level qualify only validates UNQUALIFIED columns against the schema —
    a column already qualified to a source alias (`p.SSN`) is treated as resolved
    and its SOURCE table is never checked. So a JOIN to a table absent from `uses`
    reads arbitrary columns. This closes it at the TABLE level (qualified,
    fully-qualified, and CROSS JOIN forms). A CTE name is the query's OWN derived
    table (not a warehouse source) and is skipped; a table-function / db-less source
    that is not a CTE and not in `uses` is rejected (fail-closed)."""
    cte_names = {cte.alias for cte in qualified.find_all(exp.CTE) if cte.alias}
    for table in qualified.find_all(exp.Table):
        name = table.name
        db = table.text("db")
        if not name or (not db and name in cte_names):
            continue  # a CTE reference (own derived table), never a warehouse source
        if db == _SCRATCH_DB:
            # A table-consume placeholder (scratch.<placeholder>) is a SESSION-GATED
            # source (D69/OQ-4), materialized at runtime from an already-scope-checked
            # upstream node result — not part of the warehouse `uses` footprint (§2.3).
            continue
        if name not in schema_dict.get(db, {}):
            qualified_name = f"{db}.{name}" if db else name
            raise CorpusLoadError(
                f"blueprint {bp_id}: {where} reads from source table {qualified_name!r} "
                "which is not in the declared uses footprint"
            )


def _assert_template_reads_within_uses(
    bp_id: str,
    where: str,
    tree: exp.Expression,
    uses: list[str],
    scratch_schema: dict[str, dict[str, str]] | None = None,
) -> None:
    """§1.2(c), table-aware (review FIX 1): every table + column the template reads
    must resolve to a `db.table.column` present in the declared `uses`.

    Two-level check: (1) every SOURCE table in FROM/JOIN resolves into the
    uses-schema (`_assert_source_tables_in_uses` — closes the JOIN-to-an-unlisted-
    table hole where an alias-qualified `p.SSN` reads an undeclared table); (2)
    every column qualifies against a schema built ONLY from `uses`, with
    `expand_alias_refs=False` so an output-alias name can never mask a real
    same-named column read (the alias-mask evasion). Any column that cannot be
    resolved — out of `uses`, wrong table, an alias-masked read — raises `sqlglot`'s
    `OptimizeError`, surfaced as `CorpusLoadError`.

    `*` stars and dict-family functions are rejected by the caller BEFORE this runs
    (they defeat any column-level analysis)."""
    schema_dict = _uses_schema(uses)
    # Register the session-gated scratch placeholder tables (their producing node's
    # output columns) so qualify_columns resolves `scratch.<placeholder>` columns —
    # WITHOUT adding them to the warehouse `uses` footprint (§2.3 scope-honesty).
    if scratch_schema:
        schema_dict.setdefault(_SCRATCH_DB, {}).update(scratch_schema)
    try:
        # INSIDE the try: `MappingSchema` itself raises on a malformed schema mapping
        # (e.g. an empty column map), and every failure on this path must surface as a
        # `CorpusLoadError`, never a raw sqlglot exception.
        schema = MappingSchema(schema_dict, dialect="clickhouse")
        qualified = qualify_tables(tree.copy(), dialect="clickhouse")
        _assert_source_tables_in_uses(bp_id, where, qualified, schema_dict)
        qualified = qualify_columns(
            qualified,
            schema=schema,
            expand_alias_refs=False,
            expand_stars=False,
            infer_schema=False,
            dialect="clickhouse",
        )
        validate_qualify_columns(qualified)
    except CorpusLoadError:
        raise
    except Exception as exc:  # noqa: BLE001 - any qualify failure is a fail-closed scope violation
        raise CorpusLoadError(
            f"blueprint {bp_id}: {where} reads a column outside the declared uses "
            f"footprint (or an unresolvable/cross-table reference): {exc}"
        ) from exc


def _assert_no_dict_functions(bp_id: str, where: str, tree: exp.Expression) -> None:
    """Reject dictionary-family functions (`dictGet…`) that read a ClickHouse
    dictionary source INVISIBLE to the column walk (review FIX 1 / reviewer dictGet
    case) — a hidden read outside the declared `uses`."""
    for node in tree.walk():
        if isinstance(node, exp.Anonymous):
            name = node.this or ""
            if isinstance(name, str) and name.lower().startswith("dict"):
                raise CorpusLoadError(
                    f"blueprint {bp_id}: {where} uses a dictionary function "
                    f"({name}) that reads a source invisible to scope analysis"
                )


def _validate_dag_structure(bp_id: str, blueprint: Blueprint) -> None:
    """§1.2(d): `composes` is a DAG — every `feeds_from` reference exists and there
    are no cycles. Raises `CorpusLoadError` on a dangling ref, a cycle, or an
    adversarially large DAG (FIX 3: a hard node-count cap so a huge/malicious
    composes fails LOUD with a clean error, never a stack overflow)."""
    nodes = blueprint.composes
    if not nodes:
        return
    if len(nodes) > _MAX_COMPOSE_NODES:
        raise CorpusLoadError(
            f"blueprint {bp_id}: composes has {len(nodes)} nodes, exceeding the "
            f"{_MAX_COMPOSE_NODES}-node cap (a Phase-1 blueprint DAG is small; a "
            "huge DAG is an authoring error, not a valid fast path)"
        )
    orders = [n.order for n in nodes]
    if len(orders) != len(set(orders)):
        raise CorpusLoadError(f"blueprint {bp_id}: composes has duplicate node 'order' values")
    order_set = set(orders)
    edges: dict[int, tuple[int, ...]] = {}
    for node in nodes:
        for parent in node.feeds_from:
            if parent not in order_set:
                raise CorpusLoadError(
                    f"blueprint {bp_id}: node {node.order} feeds_from unknown node {parent}"
                )
        edges[node.order] = node.feeds_from
    # Cycle detection via ITERATIVE DFS coloring over the feeds_from edges (FIX 3:
    # an explicit stack instead of recursion, so a very long forward-reference
    # chain fails with a clean CorpusLoadError rather than a RecursionError).
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(order_set, white)
    for start in order_set:
        if color[start] != white:
            continue
        stack: list[tuple[int, bool]] = [(start, True)]
        while stack:
            node_order, entering = stack.pop()
            if not entering:
                color[node_order] = black
                continue
            if color[node_order] == black:
                continue
            color[node_order] = grey
            stack.append((node_order, False))
            for parent in edges.get(node_order, ()):
                if color[parent] == grey:
                    raise CorpusLoadError(
                        f"blueprint {bp_id}: composes has a cycle at node {node_order}"
                    )
                if color[parent] == white:
                    stack.append((parent, True))


def _validate_blueprint_dag(bp: BlueprintSeed) -> None:
    """Write-time full-DAG validation (§1.2, fail-loud — mirrors
    `_validate_blueprint_uses`). An authoring mistake FAILS the seed load rather
    than shipping a silently-broken blueprint.

    Checks: (structural) the DAG parses into typed objects; (a) every `{slot}`
    token in a sql_template has a matching `slots` entry; (b) each sql_template
    parses under sqlglot ClickHouse AND is a single READ-ONLY SELECT (no DDL/DML,
    no multi-statement block — FIX 2); (c) the template's footprint ⊆ the declared
    `uses`, enforced TABLE-AWARELY (every source table AND every column resolves
    into the uses-schema — FIX 1) after rejecting the analysis-defeating constructs
    (`*` stars — FIX 1a; dict-family functions); (d) `composes` is a DAG (no cycles,
    refs exist, ≤ the node cap — FIX 3); and every `when` clause is a valid,
    entity-AGNOSTIC predicate (D59)."""
    try:
        blueprint = Blueprint.parse(
            id=bp.id,
            intent=bp.intent,
            resolves=bp.resolves,
            slots=bp.slots,
            uses_rules=bp.uses_rules,
            sql_template=bp.sql_template,
            composes=bp.composes,
            result_grain=bp.result_grain,
            # J7: threaded so the closed-set check runs at WRITE, for every write path
            # (fixture seed, MCP-export hydration, the learning landing writer all reach
            # here from `load_corpus`'s pre-write pass). The read side then never has to
            # ask whether a stored anchor is renderable.
            window_anchor=bp.window_anchor,
        )
    except BlueprintParseError as exc:
        raise CorpusLoadError(f"blueprint {bp.id}: malformed DAG — {exc}") from exc

    # B4: a blueprint with BOTH a top-level `sql_template` AND a non-empty
    # `composes` has no execution mode (the DAG path runs and the top template is
    # DEAD) — a required slot living only in the dead template would be a silent
    # dropped filter. Reject the hybrid outright (fail-loud, never ships).
    if blueprint.sql_template and blueprint.composes:
        raise CorpusLoadError(
            f"blueprint {bp.id}: declares BOTH a top-level sql_template and a "
            "composes DAG — a blueprint is EITHER single-node (sql_template) OR "
            "multi-node (composes), never both (the top-level template would be "
            "dead code and any slot it alone references a silent dropped filter)."
        )

    # A slot's LEGAL bind-site TOKENS (not its name): a scalar slot binds `{name}`,
    # a `period_range` binds `{name}_start`/`{name}_end` (the anti-drift helper —
    # identical rule the executor's B3 check uses). Undeclared-token and
    # required-referenced gates below operate on tokens, not names.
    # M1: build the token set collision-AWARE. Two slots whose tokens overlap (e.g. a
    # `string` slot literally named `w_start` alongside a `period_range` slot `w`)
    # would have ONE slot's validated bound silently shadow the other's at bind time
    # — a bypass of the per-slot validation. Fail LOUD at load.
    slot_tokens: set[str] = set()
    for slot in blueprint.slots:
        for token in slot_token_names(slot):
            if token in slot_tokens:
                raise CorpusLoadError(
                    f"blueprint {bp.id}: slot bind-token {token!r} is claimed by two slots "
                    "(a period_range expands to {name}_start/{name}_end — rename to avoid the "
                    "collision, or one slot's validated bound would silently shadow the other's)."
                )
            slot_tokens.add(token)
    # Slice C: a node template placeholder may also be a `consumes` upstream-scalar
    # binding or a `resolve_via` rule IN-list — those are NOT slots but ARE valid
    # bind sites. Collect the rule-bind placeholder names once (blueprint-wide).
    rule_binds: set[str] = set()
    for raw_rule in blueprint.uses_rules:
        parsed = parse_rule(raw_rule)
        if parsed is not None:
            rule_binds.add(parsed.binds)
    # M1 (cont.): a slot token colliding with a rule-bind IN-list name is the same
    # shadow bug across the slot/rule boundary — reject it too. (A slot/`consumes`
    # collision is checked per-node below, where the node's `consumes` set is known.)
    slot_rule_overlap = slot_tokens & rule_binds
    if slot_rule_overlap:
        raise CorpusLoadError(
            f"blueprint {bp.id}: slot bind-token(s) {sorted(slot_rule_overlap)} collide with a "
            "resolve_via rule 'binds' name — one would silently shadow the other at bind time."
        )

    nodes_by_order = {n.order: n for n in blueprint.composes}
    # (node_order | None) → the extra non-slot placeholders that node may reference.
    templates: list[tuple[int | None, str, set[str]]] = []
    if blueprint.sql_template:
        templates.append((None, blueprint.sql_template, set(rule_binds)))
    for node in blueprint.composes:
        if node.sql_template:
            allowed_extra = set(rule_binds) | set(node.consumes.keys())
            templates.append((node.order, node.sql_template, allowed_extra))

    # M1/M2 precompute: a slot token collides with a node `consumes` key (checked
    # per-node below); and a `period_range` slot's tokens must appear ALL-or-NONE in
    # each template (M2). Map each range slot's name → its two-token set once.
    range_token_sets = {
        s.name: slot_token_names(s) for s in blueprint.slots if s.type == "period_range"
    }
    for order, template, allowed_extra in templates:
        where = "sql_template" if order is None else f"node {order} sql_template"
        referenced_here = referenced_slots(template)
        # M1: a slot token that is ALSO this node's `consumes` key would shadow a
        # validated slot bound with an upstream scalar — reject (the rule-bind case
        # is caught blueprint-wide above; this is the per-node consumes case).
        consumes_collision = slot_tokens & (allowed_extra - rule_binds)
        if consumes_collision:
            raise CorpusLoadError(
                f"blueprint {bp.id}: {where} 'consumes' key(s) {sorted(consumes_collision)} "
                "collide with a slot bind-token — rename the consume or the slot."
            )
        # (a) undeclared placeholder — must be a slot TOKEN, a `consumes`, or a rule
        # bind. (A `period_range` slot contributes two tokens, so `{X_start}`/
        # `{X_end}` are legal while a bare `{X}` is NOT.)
        unknown = referenced_here - slot_tokens - allowed_extra
        if unknown:
            raise CorpusLoadError(
                f"blueprint {bp.id}: {where} references undeclared slot(s) {sorted(unknown)} "
                "(not a slot, a node 'consumes', or a resolve_via rule 'binds')"
            )
        # M2: a `period_range` is ALL-OR-NONE per template — if a template references
        # ANY of its tokens it must reference BOTH, so a range with a dropped bound is
        # rejected PER NODE (a DAG with node1 using {X_start} and node2 using {X_end}
        # binds a half-range on each — the D56 dropped-filter class). The
        # blueprint-wide "every required token referenced" gate (e) is the converse.
        for range_name, range_tokens in range_token_sets.items():
            hit = range_tokens & referenced_here
            if hit and hit != range_tokens:
                raise CorpusLoadError(
                    f"blueprint {bp.id}: {where} references only part of period_range slot "
                    f"{range_name!r} ({sorted(hit)}); it must reference BOTH "
                    f"{sorted(range_tokens)} or neither (a half-range is a dropped filter)."
                )
        # (b) parses under ClickHouse dialect + is a single read-only SELECT (FIX 2).
        try:
            tree = parse_template(template)
            assert_read_only_select(tree)
        except TemplateBindError as exc:
            raise CorpusLoadError(f"blueprint {bp.id}: {where} does not parse — {exc}") from exc
        # (c) footprint ⊆ declared uses — table-aware (FIX 1). First reject the
        # constructs that DEFEAT column-level analysis (a `*` names zero columns
        # while reading everything; a dict-family function reads a hidden source),
        # THEN qualify every source table + column against the uses allowlist.
        if contains_star(tree):
            raise CorpusLoadError(
                f"blueprint {bp.id}: {where} uses `*` — a blueprint must name its "
                "columns so its scope footprint is verifiable"
            )
        _assert_no_dict_functions(bp.id, where, tree)
        # (c0) The scratch DB must be spelled canonically, BEFORE the scope check reads
        # `db == 'scratch'` to decide what is session-gated. `SCRATCH.borrowed` otherwise
        # reads as an ordinary warehouse source and, once declared in `uses`, loads a
        # blueprint that can never run (see `_assert_canonical_scratch_spelling`).
        _assert_canonical_scratch_spelling(bp.id, where, tree)
        # A consumer node's `scratch.<placeholder>` columns are session-gated: register
        # the producing node's output columns so qualify resolves them, while the
        # warehouse columns are still checked ⊆ uses (§2.3 scope-honesty).
        scratch_schema = (
            _scratch_schema_for_node(nodes_by_order[order], nodes_by_order)
            if order is not None and order in nodes_by_order
            else None
        )
        # A producer with no `sql_template` (or one with no NAMED select aliases)
        # yields an EMPTY column map, which `MappingSchema` rejects with a raw sqlglot
        # `SchemaError` — an un-wrapped third-party exception out of `load_corpus`
        # instead of the `CorpusLoadError` its callers handle. Reject it here with the
        # actual cause: an un-schema'd scratch source cannot be scope-checked at all,
        # so this is fail-closed, not cosmetic.
        un_schemad = sorted(ph for ph, cols in (scratch_schema or {}).items() if not cols)
        if un_schemad:
            raise CorpusLoadError(
                f"blueprint {bp.id}: {where} consumes table placeholder(s) {un_schemad} "
                "whose producing node declares no derivable output columns (no "
                "sql_template, or a template with no named SELECT aliases) — the "
                "scratch JOIN cannot be scope-checked."
            )
        _assert_template_reads_within_uses(bp.id, where, tree, bp.uses, scratch_schema)

    # (e) B3(a): every declared REQUIRED slot MUST be referenced by ≥1 template —
    # the converse of the (a) token⊆slots check. An unreferenced required slot is a
    # DROPPED FILTER at execution (the query runs without the user's intended
    # constraint and returns company-wide numbers that still pass the grain gate),
    # the exact wrong-answer class D56 exists to block. Fail the seed load LOUD.
    all_referenced: set[str] = set()
    for _order, template, _extra in templates:
        all_referenced |= referenced_slots(template)
    # A required slot is satisfied iff EVERY one of its bind tokens appears (so a
    # `period_range` template referencing only `{X_start}` is REJECTED here — the
    # dropped `{X_end}` bound would be a dropped filter, aligning with the
    # executor's all-tokens B3 rule). Scalar slots (one token) are unchanged.
    unreferenced_required = {
        s.name for s in blueprint.slots if s.required and (slot_token_names(s) - all_referenced)
    }
    if unreferenced_required:
        raise CorpusLoadError(
            f"blueprint {bp.id}: required slot(s) {sorted(unreferenced_required)} are "
            "declared but referenced by NO template — an unreferenced required slot is a "
            "silent dropped filter (D56 wrong-answer class). Reference it or make it optional."
        )

    # (e2) An OPTIONAL slot whose token IS referenced by a template MUST carry an
    # `optional_pattern`: on omission the executor substitutes that pattern (e.g.
    # `TRUE`) so the template runs UNFILTERED on that dimension ("all values"). A
    # pattern-LESS referenced optional slot instead leaves its `{token}` unbound on
    # omission → the bind fails → the fast path burns to the raw loop (it does NOT
    # run unfiltered). Fail LOUD at load — mirrors the learning extractor's D97
    # optional⇒pattern gate — so the model-facing "omit = all values" note is true
    # by construction. An UNREFERENCED optional slot needs no pattern (nothing binds
    # it, so its omission is a genuine no-op — skip it).
    referenced_optional_no_pattern = sorted(
        s.name
        for s in blueprint.slots
        if not s.required
        and s.optional_pattern is None
        and (slot_token_names(s) & all_referenced)
    )
    if referenced_optional_no_pattern:
        raise CorpusLoadError(
            f"blueprint {bp.id}: optional slot(s) {referenced_optional_no_pattern} are "
            "referenced by a template but declare NO optional_pattern — on omission the "
            "token stays unbound and the run fails closed to the raw loop instead of "
            "running unfiltered. Add an optional_pattern (e.g. 'TRUE' for no filter) or "
            "make the slot required."
        )

    # (f) S1: every slot's `binds_to` MUST be within the blueprint's declared
    # `uses` footprint — otherwise the runtime DISTINCT domain probe reads a column
    # the blueprint never advertised (D88c footprint story false for probes) and a
    # binds_to⊄user-scope probe silently degrades. Asserting binds_to ⊆ uses at
    # WRITE makes an in-scope blueprint's probe provably scope-clean.
    uses_set = set(bp.uses)
    for slot in blueprint.slots:
        if slot.binds_to is not None and slot.binds_to not in uses_set:
            raise CorpusLoadError(
                f"blueprint {bp.id}: slot {slot.name!r} binds_to {slot.binds_to!r} which is "
                "NOT in the blueprint's declared uses — a slot's domain probe must read only "
                "an advertised column (add it to uses or fix binds_to)."
            )
        # (f2) Slice C: an authored `optional_pattern` MUST pass the SAME parse gate
        # the runtime applies (a self-contained boolean condition — no statement, no
        # bare literal/arithmetic, no embedded placeholder). Validating at LOAD makes
        # a malformed pattern fail LOUD here instead of silently burning a fast-path
        # attempt (→ raw loop) on every hit.
        if slot.optional_pattern is not None:
            try:
                validate_optional_pattern(slot.name, slot.optional_pattern)
            except TemplateBindError as exc:
                raise CorpusLoadError(
                    f"blueprint {bp.id}: slot {slot.name!r} has a malformed "
                    f"optional_pattern — {exc}"
                ) from exc

    # (g) S6a: a `resolve_via` rule's probed column MUST be within the declared
    # `uses` (scope-honesty for rules, matching the template footprint check) —
    # otherwise the runtime `resolve()` reads a column the blueprint never
    # advertised (D88c footprint false for rule probes).
    for raw_rule in blueprint.uses_rules:
        parsed = parse_rule(raw_rule)
        if parsed is None:
            continue
        rule_col_key = f"{parsed.table}.{parsed.column}"
        if rule_col_key not in uses_set:
            raise CorpusLoadError(
                f"blueprint {bp.id}: resolve_via rule {parsed.rule_id!r} probes "
                f"{rule_col_key!r} which is NOT in the declared uses — a rule's "
                "resolve() probe must read only an advertised column."
            )

    # (h) S6b: a node's `consumes` MUST reference a real upstream output —
    #   - SCALAR consume `$P.name`: P ∈ feeds_from AND `name` a declared SCALAR
    #     output of P (bound as a typed literal into `{placeholder}`).
    #   - TABLE consume `$P` (table-intermediate Slice 2, §2.3): P ∈ feeds_from AND
    #     P declares a `table` output, AND the `placeholder` appears as a
    #     `scratch.<placeholder>` FROM/JOIN token in this node's template (the AST
    #     JOIN-rewrite site). This is the "table-consume placeholder maps to a
    #     FROM/JOIN position" loader gate.
    # Fail LOUD at load (like feeds_from), not a generic runtime SLOT_INVALID.
    outputs_by_order = {n.order: n.output for n in blueprint.composes}
    for node in blueprint.composes:
        scratch_refs = _scratch_placeholder_names(node.sql_template)
        for placeholder, ref in node.consumes.items():
            ref_str = str(ref)
            table_match = TABLE_CONSUME_REF.match(ref_str)
            if table_match is not None:
                src_order = int(table_match.group(1))
                if src_order not in node.feeds_from:
                    raise CorpusLoadError(
                        f"blueprint {bp.id}: node {node.order} consumes a table from node "
                        f"{src_order}, which is not in its feeds_from {list(node.feeds_from)}."
                    )
                src_outputs = outputs_by_order.get(src_order, {})
                if not any(kind == "table" for kind in src_outputs.values()):
                    raise CorpusLoadError(
                        f"blueprint {bp.id}: node {node.order} consumes table {ref_str!r} but "
                        f"node {src_order} has no declared TABLE output."
                    )
                if placeholder not in scratch_refs:
                    raise CorpusLoadError(
                        f"blueprint {bp.id}: node {node.order} table-consume {placeholder!r} must "
                        f"appear as a 'scratch.{placeholder}' source in a FROM/JOIN position."
                    )
                continue
            match = SCALAR_CONSUME_REF.match(ref_str)
            if match is None:
                raise CorpusLoadError(
                    f"blueprint {bp.id}: node {node.order} consumes {placeholder!r} "
                    f"from {ref!r}, which is not a '$N.name' scalar or '$N' table reference."
                )
            src_order, out_name = int(match.group(1)), match.group(2)
            if src_order not in node.feeds_from:
                raise CorpusLoadError(
                    f"blueprint {bp.id}: node {node.order} consumes from node "
                    f"{src_order}, which is not in its feeds_from {list(node.feeds_from)}."
                )
            if outputs_by_order.get(src_order, {}).get(out_name) != "scalar":
                raise CorpusLoadError(
                    f"blueprint {bp.id}: node {node.order} consumes {ref!r} but node "
                    f"{src_order} has no declared SCALAR output named {out_name!r}."
                )

    # (i) S4: a TERMINAL approval-only node (an approval gate that is a topo SINK
    # and has no query of its own) gates nothing — on approve there is no
    # post-approval query and the pre-pause result is not carried across the pause
    # (scalar-only, F2), so it can only ABORT. Reject at load: an approval must
    # gate a downstream node (or run its own query).
    fed_from: set[int] = set()
    for node in blueprint.composes:
        fed_from.update(node.feeds_from)
    for node in blueprint.composes:
        is_approval = node.node_kind == "approval" or bool(node.requires_approval)
        is_sink = node.order not in fed_from
        if is_approval and is_sink and not node.sql_template:
            raise CorpusLoadError(
                f"blueprint {bp.id}: node {node.order} is a TERMINAL approval gate "
                "(a sink with no query) — an approval must gate a downstream node "
                "or run its own query; a terminal approval can only abort on approve."
            )

    for node in blueprint.composes:
        if node.when is not None:
            try:
                validate_when(node.when.expr)
            except WhenClauseError as exc:
                raise CorpusLoadError(
                    f"blueprint {bp.id}: node {node.order} when-clause invalid — {exc}"
                ) from exc
            # (j) S5: a `count($N)` threshold over a SCALAR-output node is
            # meaningless (a scalar node's `$N` has row_count ≤ 1 by contract, and
            # that shape drifts to a synthetic marker across a resume). Reject it so
            # the drift can never flip a gate. `empty($N)` is fine (shape-only).
            for counted in _COUNT_REF.findall(node.when.expr):
                src = int(counted)
                if any(kind == "scalar" for kind in outputs_by_order.get(src, {}).values()):
                    raise CorpusLoadError(
                        f"blueprint {bp.id}: node {node.order} when-clause applies "
                        f"count($ {src}) to a SCALAR-output node — a scalar's row "
                        "count is ≤ 1 by contract (and drifts across resume); use a "
                        "value comparison ($N.name) or empty($N) instead."
                    )

    _validate_dag_structure(bp.id, blueprint)


async def apply_schema(
    driver: AsyncDriver, *, dimension: int, database: str = "neo4j"
) -> None:
    """Create the constraints + native vector indexes (idempotent, at *dimension*),
    then wait for every index to come ONLINE so a subsequent recall sees them (§4.2).

    Part B dimension-parity: BEFORE the `CREATE VECTOR INDEX ... IF NOT EXISTS` (which
    silently keeps a pre-existing index's OLD dimension), introspect the existing
    vector-index dimensions and `check_dimension_parity` — raising
    `DimensionMismatchError` if an index already exists at a DIFFERENT dimension (the
    signal that the embedding model changed and the graph must be rebuilt). A fresh
    graph (no such index) passes and the indexes are created at *dimension*."""
    async with driver.session(database=database) as session:
        existing_dims = await _fetch_existing_vector_dims(session)
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
    """Best-effort single-flight claim on the `RebuildLock` singleton (Part C).

    Returns True iff THIS *holder* now owns the lock (it may nuke + rebuild); False
    iff another live holder holds a fresh claim (skip). Best-effort: without a
    uniqueness constraint a simultaneous MERGE could in theory double-create, and the
    nuke SPARES the lock node, so the claim protects the FULL rebuild + the stale
    window; a `rebuild_lock_id` uniqueness constraint (created here, before the MERGE)
    closes the simultaneous double-MERGE. A stale claim (holder crashed) is reclaimed
    after *stale_seconds*."""
    async with driver.session(database=database) as session:
        await session.run(_REBUILD_LOCK_CONSTRAINT)  # type: ignore[arg-type]
        result = await session.run(
            _CLAIM_REBUILD_LOCK, holder=holder, stale_seconds=stale_seconds
        )
        row = await result.single()
    return bool(row["claimed"]) if row is not None else False


async def nuke_graph(driver: AsyncDriver, *, database: str = "neo4j") -> None:
    """DESTRUCTIVE (Part C): drop the corpus vector indexes + all schema constraints,
    then `DETACH DELETE` every node — leaving a completely empty graph ready for a
    fresh `apply_schema` + reseed at a (possibly new) embedding dimension.

    Dropping the vector indexes is REQUIRED (a `CREATE ... IF NOT EXISTS` keeps the
    old dimension otherwise); deleting the nodes is REQUIRED so the B1 freshness
    singletons (`:CatalogMeta`/`:CorpusMeta`) don't short-circuit the re-seed. The
    `:RebuildLock` singleton is SPARED so the single-flight guard survives its own nuke
    (see `_NUKE_DELETE_NODES`). Strictly for the seed-script maintenance op — the SINGLETON
    hydrator does NOT call this (it would DESTROY the `source='learning'` staging tier, which
    is not in the MCP export and would not be re-seeded); the hydrator uses the scoped
    `rebuild_mcp_corpus_partition` instead."""
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

    Clears ONLY the trusted `source='mcp'` `:Blueprint`/`:KnowledgeChunk` nodes + the
    `:CorpusMeta`/`:CatalogMeta` freshness singletons, then leaves the caller to reseed.
    PRESERVES the `source='learning'` staging tier (human-promoted content not in the MCP
    export — a full `nuke_graph` would destroy it) AND the `:Table`/`:Column` catalog graph.

    Two modes:
      * *dimension* given (a DIMENSION change) — additionally DROP + recreate the two corpus
        vector indexes at the new dimension (a `CREATE ... IF NOT EXISTS` silently keeps the
        OLD dim, so the index must be dropped to reshape). The preserved learning-tier nodes
        keep their old-dim embeddings; they are excluded from the new-dim index + the recall
        source-gate, so no bad neighbours surface — they get a correct-dim embedding only
        if/when promoted.
      * *dimension* `None` (a same-dim MODEL swap) — leave the indexes; just clear the mcp
        nodes so the reseed re-embeds every mcp node at the new model WITHOUT tripping the
        (mcp-scoped) write-time model-parity guard, which reads the pre-write state.

    Deleting the mcp nodes (not merely overwriting) is REQUIRED even for a same-dim model
    swap: `load_corpus`'s parity check reads the existing mcp models as the FIRST statement
    of its write txn, so a stale old-model node would raise `CorpusLoadError` before the
    MERGE-by-id overwrite ran. Clearing first makes the reseed provably parity-clean.

    ATOMICITY (crash-safety): the mcp-node delete AND the freshness-singleton delete run in
    ONE `execute_write` transaction so they commit together. Were they two auto-commit
    statements, a crash BETWEEN them (a transient neo4j/network blip `run_forever` swallows,
    or a pod kill) could leave the mcp partition DELETED while `:CorpusMeta` SURVIVED at its
    old sha — the next cycle would see no mcp model change, take the normal path, and
    `load_corpus` would B1 sha-SKIP on the matching sha → recall permanently empty while
    /ready still reads 200 (the silent-fleet-recall-loss class). Committing both deletes
    atomically guarantees a crash leaves BOTH gone, forcing a converging reseed next cycle.
    The vector-index DDL stays OUTSIDE the txn (neo4j forbids schema ops inside a data txn)."""
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
    """Ensure ONLY the catalog-graph constraints (`Table.key`, `Column.key`,
    `CatalogMeta.id`) — the lightweight schema-ensure `load_catalog_graph` uses.

    Deliberately does NOT create the vector indexes NOR call `db.awaitIndexes(300)`:
    those are irrelevant to the `:Table`/`:Column` upsert and would add index-await
    latency to the B1 cold-fetch turn. The full `apply_schema` (with vector indexes)
    stays owned by `load_corpus`. Self-deploys the `:CatalogMeta` singleton
    constraint the graph's sha-guard relies on. Idempotent (`IF NOT EXISTS`)."""
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
    """The stamp/guard key for a hydration run — the export's own `catalog_sha`,
    or a deterministic content-hash FALLBACK when it is empty/missing (M1).

    An empty sha would silently break BOTH the skip-guard (`current == ""` never
    triggers a no-op) AND the GC predicate (`coalesce(sha,'') <> ''` matches every
    node, incl. freshly-stamped ones), so we never propagate one: a missing sha
    derives a stable SHA-1 over the `catalog` dict (sorted keys, `default=str` for
    any non-JSON value) and logs a warning. The same content always yields the same
    stamp, so the skip-guard + GC still function idempotently."""
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
    """Independent, enriched, self-healing hydration of the `:Table`/`:Column`
    catalog graph from the MCP catalog EXPORT dict (`{"catalog_sha", "catalog"}`).

    Catalog-OWNED (separate from `load_corpus`): no embeddings, no model-parity.
    Every node upsert stamps the run's `catalog_sha` (or a derived content hash when
    the export lacks one, `_effective_catalog_sha`).

    Two write modes (QA#1 — the different-sha mutual-GC race):
      * `gc=True` (default, the EXPLICIT seed/reconcile path — `scripts/seed_neo4j_
        corpus.py`): after upserting, GC deletes any `:Table`/`:Column` whose stamp
        is stale (a dropped-column reconcile), logging any GC'd column that still had
        an inbound `:USES` (a blueprint referencing a column the catalog just dropped
        — GC wins). This is a full reconcile / maintenance op.
      * `gc=False` (the ONLINE B1 self-heal wired in `app.py`): upsert + meta-stamp
        ONLY, NEVER delete. Two replicas booting on DIFFERENT shas during a rolling
        deploy then converge to a current-or-SUPERSET graph instead of GC-deleting
        each other's freshly-stamped nodes. Dropped-column garbage collection is
        deferred to the explicit seed-script maintenance op.

    B1 no-op fast path (BOTH modes): if the stored `:CatalogMeta.catalog_sha` already
    EQUALS this run's sha, returns `skipped=True` WITHOUT writing (an empty/absent
    meta ⇒ proceed). Cheap short-circuit so replicas racing on boot stay idempotent.

    Atomicity: the upserts + (optional) GCs + the meta upsert run in ONE
    `session.execute_write`, so no concurrent reader ever sees a torn graph.
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

    *dimension* (Part B) is the vector-index embedding dimension. `None` (default)
    INFERS it — from the just-embedded corpus vectors when present, else a one-shot
    probe embed (`resolve_embedding_dimension`) — so the schema is shaped to the live
    embedding model without a code edit; pass an explicit int to pin it (the app
    startup-rebuild path resolves it once and threads it here).

    Raises `CorpusLoadError` on a malformed `uses` key (S2) or a write-time
    model-parity violation (§3.3).

    Governed corpus (Phase 2): each seed carries a `source`/`verified` trust stamp
    (defaulting `mcp`/`True`, so the fixture path + existing callers write TRUSTED
    canon; the learning landing writer overrides to `learning`/`False`). These flow
    onto the node so recall's `source='mcp'` trust gate + the corpus GC can partition
    the trusted canon from the learning staging tier.

    *corpus_sha* + *gc* mirror `load_catalog_graph`'s self-healing reconcile, keyed on
    `corpus_sha` and SCOPED to `source='mcp'`:
      * every seeded node is stamped with *corpus_sha*;
      * a truthy *corpus_sha* enables the B1 no-op fast path — if the `:CorpusMeta`
        singleton already carries it, the load SKIPS (no re-embed, no write) and
        returns `skipped=True`;
      * `gc=True` (the EXPLICIT seed/reconcile op — `scripts/seed_neo4j_corpus.py`)
        additionally DELETES any `source='mcp'` node whose stamp is stale (a dropped
        blueprint/knowledge reconcile). The GC WHERE clause is `source='mcp'`-scoped,
        so it can NEVER touch a `source='learning'` staging node. `gc=False` (default;
        the ONLINE seed wired in `app.py`, and the landing writer) is additive-only.
    An empty *corpus_sha* (the landing writer, Layer-1 tests) NEVER skips and NEVER
    stamps the `:CorpusMeta` singleton — behavior is byte-identical to before Phase 2.

    *catalog* (D94 Part 3, optional dev-time aid): when a `CatalogHandle` is
    supplied, every blueprint's `uses` tables are cross-checked against it and a
    SOFT WARNING is logged per blueprint referencing an uncatalogued table (a
    seed-time early warning for the catalog/extractor skew that otherwise strands
    an `ok`+`None` result at runtime). Load ALWAYS proceeds — a blueprint may
    legitimately reference tables absent from a partial/dev catalog snapshot, so
    this never raises `CorpusLoadError` (that stays reserved for genuine
    corpus-internal-consistency failures). Omit it (default `None`) to skip the
    check silently — it is not a load precondition, and the real production safety
    is MCP-fails-closed + both catalogs in agreement, not this check (D94 record
    correction).
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
        _validate_blueprint_uses(bp)
    blueprints = resolve_blueprint_references(blueprints)
    for bp in blueprints:
        _validate_blueprint_dag(bp)

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
            existing_dims = await _fetch_existing_vector_dims(session)
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

    # Serialize the DAG properties BEFORE opening the write txn. `_dag_properties` now
    # sqlglot-parses each template to derive the `structural_key`, and CPU work inside a
    # write transaction holds neo4j locks for no reason — the computation depends only on
    # the seeds, so it belongs out here with the embedding step.
    dag_props = [_dag_properties(bp) for bp in blueprints]

    async with driver.session(database=database) as session:

        async def _write(tx: AsyncManagedTransaction) -> None:
            # S3: the parity read + check are the FIRST statements of the write
            # transaction (not a separate auto-commit read), so two concurrent
            # seeders with different models cannot both pass-then-commit —
            # whichever commits second sees the first's stamp and is refused.
            existing = await _fetch_existing_models(tx)
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


async def _fetch_existing_models(runner: Any) -> set[str]:
    """Distinct non-null `embedding_model` stamps on the TRUSTED `source='mcp'`
    partition (the hydrator-redesign scope — learning-tier nodes are excluded; see
    `_EXISTING_MODELS`). Powers BOTH load_corpus's write-txn parity check AND the
    hydrator's model-change detection.

    *runner* is anything with `.run` — a session OR a managed transaction (S3
    calls this inside the write txn). Empty-string stamps are RETAINED (not
    filtered), so a broken/unstamped mcp row surfaces as a parity conflict (N2)
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
    "load_catalog_graph",
    "load_corpus",
    "load_seed_fixtures",
    "nuke_graph",
    "rebuild_mcp_corpus_partition",
    "resolve_blueprint_references",
    "resolve_embedding_dimension",
    "schema_statements",
]
