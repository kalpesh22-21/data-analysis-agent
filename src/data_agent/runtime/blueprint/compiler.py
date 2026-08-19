"""The offline blueprint COMPILER: load-time reference inlining plus the validation
gauntlet a `BlueprintSeed` must clear before the loader may write it to the corpus.

Pure functions over seeds — no neo4j, no IO. Imports NOTHING from `runtime.retrieval`:
that direction is the pre-existing import cycle, and the loader depends on this module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from sqlglot import exp
from sqlglot.optimizer.qualify_columns import qualify_columns, validate_qualify_columns
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.schema import MappingSchema

from data_agent.corpus.seeds import BlueprintSeed, CorpusLoadError

from .models import (
    DEFAULT_NODE_KIND,
    NODE_REF_KEY,
    SCALAR_CONSUME_REF,
    TABLE_CONSUME_REF,
    Blueprint,
    BlueprintParseError,
    SlotSpec,
)
from .rules import parse_rule
from .slots import slot_token_names
from .structural_key import (
    normalize_structural_grain,
    structural_key_from_templates,
    structural_key_recipe,
)
from .template import (
    SLOT_TOKEN,
    TemplateBindError,
    assert_read_only_select,
    contains_star,
    parse_template,
    referenced_slots,
    validate_optional_pattern,
)
from .when import WhenClauseError, validate_when

_logger = logging.getLogger(__name__)


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


def validate_blueprint_uses(bp: BlueprintSeed) -> None:
    """Fail-closed with context on a malformed `uses` entry.

        The design's own highest-risk contract: a `uses` key that is not a byte-exact
        `"database.table.column"` scope key is silently dropped by the scope pre-filter at
        recall. Guard it at WRITE — every entry must be a `str` with at least 3 NON-EMPTY
        dot-separated parts, and the offending key is named.

        The CONTAINER is type-checked before the loop, and that is not cosmetic. The seed
        dataclass enforces nothing at runtime and the MCP export is a separate repo's JSON:
        `uses: 5` raised a bare `TypeError` out of `load_corpus` — the class the module docstring
        forbids, since the hydration cache retries the same poisoned entry every turn — and
        `uses: "db.t.c"` was worse than a crash: it ITERATES CHARACTER-WISE, so every downstream
        reader would see one-character "scope keys".
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
# downstream — `validate_blueprint_dag`, the structural key, the `composes_json`
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

        `slot_map` is `{<child slot name>: <parent slot name>}`, keyed by the CHILD deliberately:
        the operation is "rewrite every bind token in the child's SQL into the parent's
        vocabulary", which needs a total, single-valued function from child token to parent
        token. Keying by the parent would admit `{a: dept, b: dept}` — two parents claiming one
        child slot, an ambiguity with no correct resolution.
    """

    index: int  # position in the raw `composes` list (nodes may lack a usable `order`)
    where: str  # human-facing node label for error messages
    target: str
    slot_map: dict[str, str]


def _is_bind_token_name(name: Any) -> bool:
    """True iff `{name}` tokenizes to exactly the slot bind site *name*.

        DERIVED, never mirrored: the candidate is round-tripped through the SAME
        `referenced_slots` tokenizer the templates are read with, so this cannot drift from
        `template.SLOT_TOKEN` the way a copied regex would.

        Load-bearing for SAFETY, not tidiness. A slot-map VALUE is substituted into the child's
        SQL as the literal text `{<value>}`, so a value that is not exactly a bind token — say
        `"x} OR 1=1 --"` — would splice attacker-chosen raw SQL into a template that is then
        parsed and executed. This round-trip is the boundary that keeps the substitution a
        RENAME instead of an injection.
    """
    return isinstance(name, str) and referenced_slots("{" + name + "}") == {name}


def _seed_compose_nodes(bp: BlueprintSeed) -> list[dict[str, Any]]:
    """*bp*'s raw `composes` entries, with the CONTAINER type-checked first.

        `Blueprint.parse` rejects a non-list too, but that runs later and reference resolution
        has to walk the nodes before then. A non-iterable would raise a bare `TypeError` out of
        `load_corpus`; a STRING would iterate CHARACTER-WISE and look like a perfectly valid
        zero-reference DAG, which is the quieter and worse failure. A non-dict ENTRY is passed
        through untouched — it carries no reference, and `Node.parse` owns that error message.
    """
    if bp.composes is None:
        return []
    if not isinstance(bp.composes, (list, tuple)):
        raise CorpusLoadError(
            f"blueprint {bp.id}: 'composes' must be a list, got {type(bp.composes).__name__}"
        )
    return list(bp.composes)


def _seed_slot_specs(bp: BlueprintSeed) -> dict[str, SlotSpec]:
    """*bp*'s declared slots as `{name: SlotSpec}`, parsed EARLY because reference resolution
        needs each slot's bind TOKENS — a `period_range` occupies two, everything else one.

        Wraps `BlueprintParseError` as `CorpusLoadError` and re-checks the container type for the
        same reason as `_seed_compose_nodes`. Duplicate names are rejected here as well as in
        `Blueprint.parse`: this builds a dict, and a silent last-wins overwrite would make the
        reference rename pick one of two colliding specs arbitrarily.
    """
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
    """The `resolve_via` rules *bp* declares, keyed by the `{token}` each binds.

        Keyed by BIND rather than by `id` because the bind name is what a template references and
        therefore what a reference has to reconcile. The rule OBJECT is kept, not just the name,
        so the caller can compare what two same-named rules actually probe. `parse_rule` is
        total, so the only guard needed is the container type.
    """
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

        Every field is untrusted, and every check below is derived from what the value is LATER
        USED FOR, not from its name:

        | value                | downstream use                          | guard              |
        |----------------------|-----------------------------------------|--------------------|
        | `ref`                | `.get()` of two known keys              | must be a dict     |
        | extra `ref` keys     | nothing — silently ignored              | whitelist, reject  |
        | `ref.blueprint`      | key into the `{id: seed}` dict          | non-empty `str`    |
        | `ref.slots`          | `.items()`, key lookups, set algebra    | must be a dict     |
        | `ref.slots` keys     | matched against child slot NAMES        | bind-token shaped  |
        | `ref.slots` values   | emitted into SQL as the text `{value}`  | bind-token shaped  |

        The `ref.blueprint` guard is the unhashable-value case: `blueprint: [a, b]` reaches
        `by_id[...]` and raises an un-wrapped `TypeError` out of `load_corpus`. The `ref.slots`
        VALUE guard is the injection boundary (see `_is_bind_token_name`). Extra keys are
        rejected rather than ignored because the realistic authoring mistake is `slot:` for
        `slots:`, which would otherwise resolve to "no mappings declared".
    """
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

        A reference to an id absent from THIS load is fatal. "Absent" covers deleted, renamed,
        never-authored, and — because the whole load is one fail-closed unit — a child that is
        itself unloadable for any other reason, so a composite can never ship holding SQL from a
        blueprint that did not load.

        THE AVAILABILITY TRADE, stated plainly: `_seeds_from_entries` SKIPS a malformed export
        entry precisely so one bad entry cannot brick the corpus, and this function then fails
        the ENTIRE load when a reference points at an id that entry would have supplied — and
        the hydrator deliberately does not catch it. So a skipped child transitively does what
        the skip exists to prevent.

        Considered and DECLINED: skipping the referencing parent too, reserving whole-load
        failure for a genuinely never-authored id. It would make the loader tolerant of exactly
        ONE of the many ways a bad entry aborts the load, so it buys a special case rather than a
        property. If corpus availability becomes a first-class goal, do it uniformly with a
        per-blueprint quarantine in `load_corpus`, not here.
    """
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

        Iterative DFS colouring with an explicit stack — the same shape, and for the same reason,
        as `_validate_dag_structure`'s intra-DAG check: a long reference chain must fail as a
        clean `CorpusLoadError`, never as a `RecursionError` escaping `load_corpus`. That check
        is scoped to ONE blueprint's `feeds_from` edges and structurally cannot see A->B->A; this
        is its independent cross-blueprint sibling.

        Iteration order follows the seed list, so the reported cycle is deterministic.
    """
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
    """Refuse to inline from a child in a different trust partition, or from one recall would
        refuse to serve.

        Both gates are DERIVED from `vector_index._BLUEPRINT_RECALL_QUERY`, the only place the
        corpus defines "servable". Inlining copies the child's SQL into the parent, so a
        retracted or learning-tier child would keep running under the parent's id — retraction
        that does not retract, and a trust-partition crossing no reader could see.

        The `coalesce` halves are mirrored EXACTLY, because `_UPSERT_BLUEPRINT` does
        `SET b.status = $status` and neo4j REMOVES a property set to null: a Python `None`
        becomes an ABSENT property, which recall coalesces to `validated` and SERVES. Reading
        `None` as `""` failed the equality and refused the load with a message claiming recall
        would not serve it — false, and reachable from the export, so a servable child would have
        bricked the whole corpus. `""` is a DIFFERENT case and stays refused: it is written as a
        real property, coalesce leaves it alone, and it matches neither partition. Drift needs no
        coalesce — the test is `== 'suspect'`, which `None` already fails.
    """
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

        A leaf (`sql_template`, no `composes`) contributes it directly. A single-node `composes`
        contributes its one node's template — that shape is what makes a reference CHAIN possible
        at all, since a leaf carries no nodes and therefore no `ref`.

        Everything the child's node declares BESIDES the SQL is REFUSED rather than dropped,
        because the referencing node keeps its own. The control-flow trio — `node_kind`, `when`,
        `requires_approval` — is what matters: silently discarding a gate is how an approval
        pause disappears. `node_kind` is a gate ON ITS OWN, not a modifier of `requires_approval`
        (`executor._execute_dag` pauses on either), so anything PRESENT and not `"query"` is
        refused; absent is fine. The check is `!= "query"` rather than `== "approval"` so a
        future `NODE_KINDS` member is refused by default instead of waved through.

        `output` IS ignored, deliberately and alone: it describes what a node hands to a
        DOWNSTREAM sibling, a one-node DAG has none, and the referencing node declares its own.

        Called only after the child has itself been resolved (child-first order), so its node
        template is already inlined if it was a reference.
    """
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
    """The TOTAL `{child token}` -> `{parent token}` map for one reference.

        Total is the whole point: every bind token the child's template references gets an entry
        (identity for a rule bind), so the substitution can never leave a token behind for a later
        gate to trip over with a confusing message.

        SLOT-COLLISION SEMANTICS, and why each is what it is:

        * NO IMPLICIT IDENTITY. A child slot is bound ONLY through an explicit `ref.slots` entry,
          even when the two names are identical. Slots are resolved ONCE per blueprint before the
          DAG walk, so after inlining the child's slot DECLARATIONS are gone — type, `binds_to`,
          `enum_values`, all of it — and the PARENT's same-named slot governs. Implicit binding
          would let renaming a parent slot silently re-point a child's filter at a different
          domain.
        * CHILD NEEDS A SLOT THE PARENT DOES NOT SUPPLY -> refuse, naming the slots. The
          alternative is a `{token}` with nothing to bind it, i.e. a dropped filter (D56).
        * PARENT MAPS A SLOT THE CHILD DOES NOT HAVE, OR DOES NOT USE -> refuse. A dead mapping
          is an author believing a filter is applied when it is not.
        * BIND ARITY must match -> refuse on mismatch. A `period_range` occupies two tokens and
          everything else one, so mapping a range onto a scalar emits tokens nothing binds.
        * BIND TYPE and `binds_to` may differ -> WARN, do not refuse. The parent is the authority
          on its own slots and the value still binds as a typed AST literal, so a divergence is a
          resolution-strictness difference, not a safety one — but the usual cause is a
          copy-paste that will validate values against the wrong domain.
        * A `resolve_via` RULE BIND IS NOT RENAMED, and the parent must re-declare the rule
          itself — see the residual-token branch, which also warns when two same-named rules
          probe different things.
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
    """THE SECURITY GATE. A composite must DECLARE at least the union of its referenced
        blueprints' footprints, or the load fails. Not a warning.

        `uses` is hand-AUTHORED, never derived from the SQL, and it is the corpus's only
        machine-readable statement of what a blueprint reads. Three readers act on it: the recall
        scope pre-filter drops a blueprint whose `uses` is not a subset of the caller's
        `column_scope`; `promotion/token_minter.mint(column_scope=<uses>)` mints the
        golden-replay JWT from it verbatim; and DAG gate (c) checks every template against it. A
        composite that under-declares is offered to users whose scope does not cover what it
        actually reads — the pre-filter's whole job, silently defeated.

        Gate (c) DOES independently re-check the inlined SQL against the parent's `uses`, so this
        is not the only thing between a reference and a scope escape. The union rule is stricter
        on purpose: it binds the parent to the child's DECLARED footprint rather than to whatever
        columns the child's SQL happens to name today, so a later widening of the child cannot
        quietly widen every composite that inlines it.

        TRANSITIVITY: *footprint* accumulates `declared ∪ ⋃ children` in child-first order, so a
        grandchild's columns reach the grandparent even though only direct children are
        inspected.
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

        Pure and hermetic (no I/O, no driver, no embedder) — `load_corpus` calls it as the first
        step of its pre-write pass, so every write path (fixture seed, MCP export hydration, the
        learning landing writer) goes through exactly this. Input seeds are never mutated; a
        blueprint with no references is returned as-is, by identity.

        Order of operations: build and shape-validate the reference graph (a dangling target
        fails here); order it CHILD-FIRST, failing on a cross-blueprint cycle; cap the chain
        depth; then resolve in that order, so a child is already inlined when its parent reads
        it, checking the `uses` union per parent against the accumulated footprint.

        Returns the seeds in the ORIGINAL input order — `load_corpus` zips the returned list
        against its embedding vectors, and a reordered list would silently mis-pair them.

        Raises only `CorpusLoadError`. That is a hard requirement, not a style preference: this
        runs inside `load_corpus`, which the hydrator's self-heal poll re-arms and retries every
        turn, so an un-wrapped exception here bricks the corpus indefinitely.
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

        Not redundant with `load_corpus`'s pre-write pass: this is the set the `uses` UNION rule
        compares, and building it from an unvalidated `uses` is how a string silently becomes a
        handful of one-character "scope keys".
    """
    validate_blueprint_uses(bp)
    return frozenset(bp.uses)


def _compose_node_templates(composes: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """The `(order, sql_template)` pairs of a composite seed's DAG nodes — the shape the shared
        canonicalizer joins in ascending order.

        A node with no `sql_template` (canon authors output-only DAG nodes) or a non-integer
        `order` is SKIPPED, mirroring the learning side where every `NodeTemplate` carries a real
        template, so both paths join the same set of normalized strings.
    """
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

        An EXPLICIT `bp.structural_key` wins; absent one it is DERIVED from the seed's own
        templates + `result_grain`. Both branches run the SAME `structural_key_from_templates`
        helper, so they agree by construction — the learning landing seed stamps its key up front
        only to save a second sqlglot parse.

        FAIL-SOFT (D52): an unparseable template yields `""` and a WARNING, never a raise — but
        that branch is defensive DEPTH, not active load-path behaviour: `validate_blueprint_dag`
        runs unconditionally in the earlier pre-write pass and aborts the whole load for anything
        this recipe would also reject. The guard goes live only if the key recipe ever grows
        stricter than loader validation, which a test watches for.
    """
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


def dag_properties(bp: BlueprintSeed) -> dict[str, Any]:
    """Serialize the additive full-DAG fields into the neo4j string properties. Empty
        structures are stored as `null`, so a DAG-less blueprint carries no phantom `{}`/`[]`.

        `structural_key` follows the same null-when-absent rule, and that is SAFETY-RELEVANT
        rather than cosmetic: an empty-string key stored on every unparseable blueprint would make
        a naive `MATCH (b {structural_key: $k})` lookup match them ALL as false prior art.

        `structural_key_recipe` is written ONLY alongside a real key, under the same rule — a
        recipe stamp on a keyless node describes nothing. Unread today; it exists so a sqlglot
        bump splitting the re-derived canon tier from the write-once learning tier is DETECTABLE
        rather than silent.
    """
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
    """The set of `scratch.<placeholder>` table names a template references in a FROM/JOIN
        position. Parsed via `parse_template` so a `{slot}` template parses too; a non-parsing
        template yields an empty set.

        CASE-SENSITIVE, and that is the correct polarity HERE even though the sibling recognizer
        below is not. This answers "which placeholders will the executor REWRITE", and
        `template._rewrite_scratch_tables` matches `db == 'scratch'` exactly — so gate (h) must
        use the same exact test or it would accept a spelling the executor cannot rewrite.
        Case-folding here would LOOSEN gate (h) while tightening every other caller.
    """
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
    """Every `(db_as_written, table)` source in a PARSED template whose database is the scratch
        DB under a CASE-INSENSITIVE match.

        The single tree-level definition of "this source is session scratch", shared by its two
        callers so they cannot drift. Their agreement IS the load-bearing property of the scratch
        split: the canonical-spelling gate is only sound if it recognizes exactly the sources the
        reference gate does.
    """
    return [
        (table.text("db"), table.name)
        for table in tree.find_all(exp.Table)
        if table.text("db").casefold() == _SCRATCH_DB and table.name
    ]


def _scratch_db_sources(sql_template: str | None) -> list[tuple[str, str]]:
    """Every `(db_as_written, table)` source whose database is the scratch DB under a
        CASE-INSENSITIVE match — the "does this template touch session scratch at all?" question,
        deliberately over-approximating.

        The authority for that question is the MCP's own `service._references_scratch_db`, which
        matches with `re.IGNORECASE` specifically so a spelling cannot route a query around the
        session gate. This mirrors that polarity; the exact-match sibling above answers a
        different question.

        Returns the db text AS WRITTEN so a caller can name the offending spelling.
    """
    if not sql_template:
        return []
    try:
        tree = parse_template(sql_template)
    except TemplateBindError:
        return []
    return _scratch_db_sources_in_tree(tree)


def _assert_canonical_scratch_spelling(bp_id: str, where: str, tree: exp.Expression) -> None:
    """Reject a scratch-database source spelled anything other than `scratch`.

        This is what keeps the two recognizers above from disagreeing on anything that actually
        loads: after this gate, "recognized case-insensitively" and "recognized exactly" describe
        the same set, so gate (h), the executor's rewrite and the reference gate cannot diverge.

        Without it the escape is real and not merely cosmetic. sqlglot does NOT normalize
        identifier case on this path, so `FROM SCRATCH.borrowed` reads as an ordinary warehouse
        source; declare `SCRATCH.borrowed.<col>` in `uses` — which passes the `db.table.column`
        grammar unchanged — and the whole corpus loads, yielding a `validated` blueprint reading a
        session-scoped table nothing can materialize for it, and an offline golden-replay JWT
        minted from a scope containing a scratch key. The runtime still fails closed via the MCP's
        own IGNORECASE gate, so this is defence in depth — but a blueprint that cannot run should
        not load.
    """
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
    """Assert every SOURCE table (FROM/JOIN) resolves to a `(db, table)` present in the
        uses-schema.

        The column-level qualify only validates UNQUALIFIED columns: a column already qualified
        to a source alias (`p.SSN`) is treated as resolved and its SOURCE table is never checked,
        so a JOIN to a table absent from `uses` reads arbitrary columns. This closes it at the
        TABLE level (qualified, fully-qualified and CROSS JOIN forms). A CTE name is the query's
        OWN derived table and is skipped; a table-function or db-less source that is not a CTE is
        rejected fail-closed.
    """
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
    """Every table + column the template reads must resolve to a `db.table.column` present in
        the declared `uses`.

        Two levels: (1) every SOURCE table in FROM/JOIN resolves into the uses-schema, closing
        the JOIN-to-an-unlisted-table hole where an alias-qualified `p.SSN` reads an undeclared
        table; (2) every column qualifies against a schema built ONLY from `uses`, with
        `expand_alias_refs=False` so an output-alias name can never mask a real same-named column
        read. Any column that cannot be resolved raises sqlglot's `OptimizeError`, surfaced as
        `CorpusLoadError`.

        `*` stars and dict-family functions are rejected by the caller BEFORE this runs — they
        defeat any column-level analysis.
    """
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
    """Reject dictionary-family functions (`dictGet…`), which read a ClickHouse dictionary
        source INVISIBLE to the column walk — a hidden read outside the declared `uses`.
    """
    for node in tree.walk():
        if isinstance(node, exp.Anonymous):
            name = node.this or ""
            if isinstance(name, str) and name.lower().startswith("dict"):
                raise CorpusLoadError(
                    f"blueprint {bp_id}: {where} uses a dictionary function "
                    f"({name}) that reads a source invisible to scope analysis"
                )


def _validate_dag_structure(bp_id: str, blueprint: Blueprint) -> None:
    """`composes` is a DAG: every `feeds_from` reference exists and there are no cycles. Raises
        `CorpusLoadError` on a dangling ref, a cycle, or a DAG past the hard node-count cap, so a
        huge or malicious `composes` fails LOUD rather than overflowing the stack.
    """
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


def validate_blueprint_dag(bp: BlueprintSeed) -> None:
    """Write-time full-DAG validation, fail-loud: an authoring mistake FAILS the seed load
        rather than shipping a silently-broken blueprint.

        Checks: the DAG parses into typed objects; (a) every `{slot}` token in a `sql_template`
        has a matching `slots` entry; (b) each template parses under sqlglot ClickHouse AND is a
        single READ-ONLY SELECT; (c) the template's footprint is a subset of the declared `uses`,
        enforced TABLE-AWARELY, after rejecting the analysis-defeating constructs (`*` stars,
        dict-family functions); (d) `composes` is a DAG within the node cap; and every `when`
        clause is a valid, entity-AGNOSTIC predicate (D59).
    """
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


__all__ = [
    "dag_properties",
    "resolve_blueprint_references",
    "validate_blueprint_dag",
    "validate_blueprint_uses",
]
