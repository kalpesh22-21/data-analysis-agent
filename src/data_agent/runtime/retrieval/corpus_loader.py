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

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from sqlglot import exp
from sqlglot.optimizer.qualify_columns import qualify_columns, validate_qualify_columns
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.schema import MappingSchema

from data_agent.runtime.blueprint.models import Blueprint, BlueprintParseError
from data_agent.runtime.blueprint.rules import parse_rule
from data_agent.runtime.blueprint.template import (
    TemplateBindError,
    assert_read_only_select,
    contains_star,
    parse_template,
    referenced_slots,
)
from data_agent.runtime.blueprint.when import WhenClauseError, validate_when

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
    # --- additive full-DAG fields, the runBlueprint brick (OQ-T1, §1.2). All
    # optional-defaulted so existing D87/D88 fixtures still load (no migration).
    # Stored as JSON-string properties on the `:Blueprint` node; unread by recall.
    resolves: dict[str, str] = field(default_factory=dict)
    slots: list[dict[str, Any]] = field(default_factory=list)
    uses_rules: list[Any] = field(default_factory=list)
    sql_template: str | None = None
    composes: list[dict[str, Any]] = field(default_factory=list)
    result_grain: list[str] | dict[str, Any] | None = None


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


# Hard cap on `composes` DAG size (FIX 3). Phase-1 blueprints are single-node or a
# handful of scalar-converging nodes; anything beyond this is an authoring error /
# adversarial input and is rejected LOUD (never traversed into a stack overflow).
_MAX_COMPOSE_NODES = 64

# A `consumes` upstream-output reference (`$3.company_avg`) and a `count($N)`
# occurrence in a `when` expr — used by the Slice-C load-time validations.
_CONSUME_REF = re.compile(r"^\$(\d+)\.([A-Za-z_][A-Za-z0-9_]*)$")
_COUNT_REF = re.compile(r"count\(\s*\$(\d+)")
# A TABLE consume reference (`$1`) — node 1's WHOLE table output, injected as a
# `scratch.<placeholder>` FROM/JOIN token (table-intermediate Slice 2, §2.3).
_TABLE_CONSUME_REF = re.compile(r"^\$(\d+)$")
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
    b.hit_count = coalesce(b.hit_count, 0),
    b.resolves_json = $resolves_json,
    b.slots_json = $slots_json,
    b.uses_rules_json = $uses_rules_json,
    b.sql_template = $sql_template,
    b.composes_json = $composes_json,
    b.result_grain_json = $result_grain_json
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


def _dag_properties(bp: BlueprintSeed) -> dict[str, Any]:
    """Serialize the additive full-DAG fields into the neo4j string properties
    (§1.1). Empty structures are stored as `null` so a DAG-less blueprint carries
    no phantom `{}`/`[]` — additive and back-compatible with D87/D88 seeds."""
    return {
        "resolves_json": json.dumps(bp.resolves) if bp.resolves else None,
        "slots_json": json.dumps(bp.slots) if bp.slots else None,
        "uses_rules_json": json.dumps(bp.uses_rules) if bp.uses_rules else None,
        "sql_template": bp.sql_template,
        "composes_json": json.dumps(bp.composes) if bp.composes else None,
        "result_grain_json": (
            json.dumps(bp.result_grain) if bp.result_grain is not None else None
        ),
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
    empty set (the other load checks surface the parse failure)."""
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
        match = _TABLE_CONSUME_REF.match(str(ref))
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
    schema = MappingSchema(schema_dict, dialect="clickhouse")
    try:
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

    slot_names = {s.name for s in blueprint.slots}
    # Slice C: a node template placeholder may also be a `consumes` upstream-scalar
    # binding or a `resolve_via` rule IN-list — those are NOT slots but ARE valid
    # bind sites. Collect the rule-bind placeholder names once (blueprint-wide).
    rule_binds: set[str] = set()
    for raw_rule in blueprint.uses_rules:
        parsed = parse_rule(raw_rule)
        if parsed is not None:
            rule_binds.add(parsed.binds)

    nodes_by_order = {n.order: n for n in blueprint.composes}
    # (node_order | None) → the extra non-slot placeholders that node may reference.
    templates: list[tuple[int | None, str, set[str]]] = []
    if blueprint.sql_template:
        templates.append((None, blueprint.sql_template, set(rule_binds)))
    for node in blueprint.composes:
        if node.sql_template:
            allowed_extra = set(rule_binds) | set(node.consumes.keys())
            templates.append((node.order, node.sql_template, allowed_extra))

    for order, template, allowed_extra in templates:
        where = "sql_template" if order is None else f"node {order} sql_template"
        # (a) undeclared placeholder — must be a slot, a `consumes`, or a rule bind.
        unknown = referenced_slots(template) - slot_names - allowed_extra
        if unknown:
            raise CorpusLoadError(
                f"blueprint {bp.id}: {where} references undeclared slot(s) {sorted(unknown)} "
                "(not a slot, a node 'consumes', or a resolve_via rule 'binds')"
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
        # A consumer node's `scratch.<placeholder>` columns are session-gated: register
        # the producing node's output columns so qualify resolves them, while the
        # warehouse columns are still checked ⊆ uses (§2.3 scope-honesty).
        scratch_schema = (
            _scratch_schema_for_node(nodes_by_order[order], nodes_by_order)
            if order is not None and order in nodes_by_order
            else None
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
    unreferenced_required = {
        s.name for s in blueprint.slots if s.required and s.name not in all_referenced
    }
    if unreferenced_required:
        raise CorpusLoadError(
            f"blueprint {bp.id}: required slot(s) {sorted(unreferenced_required)} are "
            "declared but referenced by NO template — an unreferenced required slot is a "
            "silent dropped filter (D56 wrong-answer class). Reference it or make it optional."
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
            table_match = _TABLE_CONSUME_REF.match(ref_str)
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
            match = _CONSUME_REF.match(ref_str)
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
                if any(
                    kind == "scalar" for kind in outputs_by_order.get(src, {}).values()
                ):
                    raise CorpusLoadError(
                        f"blueprint {bp.id}: node {node.order} when-clause applies "
                        f"count($ {src}) to a SCALAR-output node — a scalar's row "
                        "count is ≤ 1 by contract (and drifts across resume); use a "
                        "value comparison ($N.name) or empty($N) instead."
                    )

    _validate_dag_structure(bp.id, blueprint)


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
    # blueprint the scope filter will always drop. The full-DAG validation (§1.2)
    # runs in the same pre-write pass so an authoring mistake never ships.
    for bp in blueprints:
        _validate_blueprint_uses(bp)
        _validate_blueprint_dag(bp)

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
                    **_dag_properties(bp),
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
