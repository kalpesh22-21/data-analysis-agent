"""S3 PLAN + S4 `BlueprintGeneralization` → runtime `Blueprint` / `BlueprintSeed`.

Contract A: a validated candidate promotes 1:1 onto `runtime/blueprint/models.py::Blueprint`
with ZERO field invention. `blueprint_from_generalization` builds the runtime `Blueprint`
that is executed live; `blueprint_seed_from_candidate` builds the parallel `BlueprintSeed`
the S9 landing writer MERGE-upserts into the neo4j retrieval corpus. Both share the SAME
slot + composes projection helpers, so the landed seed and the executable blueprint can
never drift.
"""

from __future__ import annotations

from typing import Any

from ...runtime.blueprint.models import Blueprint
from ...runtime.blueprint.structural_key import structural_key_from_templates
from ...runtime.retrieval.corpus_loader import BlueprintSeed, KnowledgeSeed
from ..candidate.generalization import BlueprintGeneralization
from ..candidate.models import CandidateEnvelope
from ..leakage.gate import _collect_text


def _slot_docs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Project the S3 `parameterization` entries onto the `slots_json` shape.

    Every `role=slot` param's `slot` dict — the SINGLE slot projection reused by both the
    runtime `Blueprint` and the landed `BlueprintSeed`. Reads the raw plan defensively, because
    this module's documented failure mode is `BlueprintParseError` (fail-closed landing), never
    a `TypeError`. A skipped malformed entry cannot smuggle anything through: the slot it would
    have declared is then missing, so its `{token}` is an undeclared placeholder the corpus
    loader refuses at the landing write.
    """
    params = payload.get("parameterization")
    if not isinstance(params, list):
        return []
    return [
        p["slot"]
        for p in params
        if isinstance(p, dict) and p.get("role") == "slot" and p.get("slot")
    ]


# The `composes_json` field ALLOWLIST — exactly the keys `runtime/blueprint/models.py
# ::Node.parse` reads (`sql_template` is joined in from S4, not copied from the plan).
# An ALLOWLIST, not the PLAN dict minus the fields we happen to dislike today: the S3
# `ComposeNodePlan` is session-scoped working state, so a field added there must land
# in the governed corpus only by an explicit decision here, never by default.
_NODE_DOC_FIELDS: tuple[str, ...] = (
    "order",
    "node_kind",
    "feeds_from",
    "consumes",
    "output",
    "when",
    "requires_approval",
)


def _compose_docs(
    payload: dict[str, Any], generalization: BlueprintGeneralization
) -> list[dict[str, Any]]:
    """Project the S3 `composes` DAG ⨝ the S4 per-node `sql_template` (by `order`).

    The SINGLE composes projection reused by both the runtime `Blueprint` and the landed seed.
    Only `_NODE_DOC_FIELDS` are carried, and two PLAN fields are deliberately dropped:
    `source_tool_call_ref`, a SESSION-scoped identifier that has no place on a globally
    recallable node (D17), and `step_intent`, free NL that is NOT one of the S5
    `_ENTITY_FREE_SURFACES` the leakage gate scans — landing it would put UNSCANNED model prose
    into the global corpus, and the last-gate tripwire can only match spans S5 already found.
    """
    template_by_order = {n.order: n.sql_template for n in generalization.node_templates}
    composes: list[dict[str, Any]] = []
    for node in payload.get("composes", []) or []:
        mapped = {key: node[key] for key in _NODE_DOC_FIELDS if key in node}
        mapped["sql_template"] = template_by_order.get(node.get("order"))
        composes.append(mapped)
    return composes


def blueprint_from_generalization(
    payload: dict[str, Any],
    generalization: BlueprintGeneralization,
    *,
    id: str,
) -> Blueprint:
    """Assemble the runtime `Blueprint` from the S3 payload + the S4 generalization.

    Raises `BlueprintParseError` if any mapped field is malformed. `window_anchor` (J7/J7c) is
    read from the S3 PLAN and is INERT until the extractor learns to declare it; it is threaded
    anyway because the alternative failure is silent — the first learned WINDOWED blueprint
    would otherwise promote anchor-less, with nothing failing anywhere.
    """
    slots = _slot_docs(payload)
    composes = _compose_docs(payload, generalization)

    return Blueprint.parse(
        id=id,
        intent=payload.get("intent", ""),
        resolves=payload.get("resolves"),
        slots=slots or None,
        uses_rules=list(generalization.uses_rules),
        sql_template=generalization.sql_template,
        composes=composes or None,
        result_grain=generalization.result_grain.to_doc(),
        window_anchor=payload.get("window_anchor"),
    )


def blueprint_seed_from_candidate(
    env: CandidateEnvelope, *, id: str, verified: bool = False
) -> BlueprintSeed:
    """Project a validated candidate onto the neo4j-corpus `BlueprintSeed` (S9 §3.2).

    Reuses `blueprint_from_generalization` to VALIDATE + normalize the structure, then projects
    only generalized, entity-free fields — never `evidence`, audit spans, or any entity-bearing
    payload (D17); the writer additionally asserts the seed is entity-free before any neo4j
    write. `id` is the deterministic landing id, so a re-promotion MERGEs in place. `verified`
    is the Phase-3 human-approval flag (auto-landed False, human-approved True) while `source`
    stays `"learning"` either way. Raises `ValueError` when the candidate carries no
    `generalization`, and `BlueprintParseError` when the structure is malformed — fail-closed,
    never a silent broken landing.
    """
    gen_doc = env.payload.get("generalization")
    if not isinstance(gen_doc, dict):
        raise ValueError(
            f"candidate {env.candidate_id} has no generalization; cannot build a landing seed"
        )
    gen = BlueprintGeneralization.from_doc(gen_doc)
    blueprint = blueprint_from_generalization(env.payload, gen, id=id)

    return BlueprintSeed(
        id=id,
        intent=blueprint.intent,
        slots_summary=", ".join(s.name for s in blueprint.slots),
        uses=list(gen.uses),
        status="validated",
        drift_status=env.drift.status,
        # Provenance (review S3): a loop-landed blueprint is distinguishable from a
        # hand-authored fixture (`created_by="seed"`) and carries its originating
        # candidate id, so incident response can find everything the loop landed.
        created_by="learning",
        source_candidate_id=env.candidate_id,
        # Governed-corpus trust partition (Phase 2, SAFETY-CRITICAL): a loop-landed
        # blueprint lands in the LEARNING STAGING tier, NOT the trusted MCP canon.
        # `source="learning"` keeps it OUT of the agent recall (the `source='mcp'`
        # trust gate) and out of the MCP-scoped corpus GC; `verified=False` is the
        # safe default; a human-approve landing threads `verified=True` here (Phase-3
        # triage). Without this explicit override the `BlueprintSeed` default
        # (`mcp`/`True`) would leak unvetted learning output straight into the trusted
        # recall partition.
        source="learning",
        verified=verified,
        # The LOOSE cross-tier identity (`runtime/blueprint/structural_key.py`). Hashing
        # only `[grain_columns, structural_ast_norm]` is what lets a landed learning node
        # match a hand-authored MCP-canon blueprint, which the frozen D48 key cannot do
        # (canon carries neither `resolves` nor `uses_rules`).
        #
        # Derived from S4's TEMPLATES, deliberately NOT from `gen.canonical_ast_norm`: the
        # structural render additionally folds standard function names and strips comments,
        # which the frozen render must never do (its digests are persisted). Passing the
        # frozen string here would mint a key the canon tier can never match. This is the
        # SAME call the seeder makes for a canon blueprint, which is what makes the two
        # tiers agree; stamping it here rather than leaving the loader to re-derive just
        # saves the second parse. Empty (⇒ absent, never colliding) when the templates do
        # not normalize.
        structural_key=structural_key_from_templates(
            gen.result_grain.to_doc(),
            gen.sql_template,
            [(n.order, n.sql_template) for n in gen.node_templates],
        ),
        resolves=dict(blueprint.resolves),
        slots=_slot_docs(env.payload),
        uses_rules=list(gen.uses_rules),
        sql_template=gen.sql_template,
        composes=_compose_docs(env.payload, gen),
        result_grain=gen.result_grain.to_doc(),
        # J7c — taken from the PARSED blueprint, not re-read from the payload, so the
        # landed seed can only ever carry a value `Blueprint.parse` already accepted
        # against the closed `WINDOW_ANCHORS` set (the corpus loader's write-time
        # `_validate_blueprint_dag` rejects anything else anyway; agreeing here means
        # a malformed anchor fails at build, not at the neo4j write).
        window_anchor=blueprint.window_anchor,
    )


def knowledge_seed_from_candidate(
    env: CandidateEnvelope, *, id: str, verified: bool = False
) -> KnowledgeSeed:
    """Project an approved global-knowledge candidate onto the neo4j-corpus `KnowledgeSeed`.

    The knowledge-side mirror of `blueprint_seed_from_candidate`, and a pure function. Reads
    ONLY the entity-free surfaces the leakage gate scans (`statement`, `structured`,
    `related_terms`, `scope`) — never `evidence`, audit spans, or any entity-bearing payload
    (D17). `id` is the deterministic landing id so a re-promotion MERGEs the same node in place;
    `verified` threads the human-approval flag and `source` stays `"learning"`. Raises
    `ValueError` when `statement` is empty: an empty chunk recalls nothing and only pollutes the
    index.
    """
    payload = env.payload
    statement = payload.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError(
            f"candidate {env.candidate_id} has an empty knowledge statement; "
            "cannot build a landing seed"
        )

    parts: list[str] = [statement.strip()]
    # `related_terms` and `structured` are landed into the SCOPE-BYPASSED knowledge
    # index (`searchKnowledge` applies no column-scope filter), so the seed text must
    # contain ONLY what the S5 leakage gate + the entity strip actually cover. The gate
    # scans string LEAF VALUES via `_collect_text` (dict KEYS and non-string scalars —
    # numbers, bools — are never scanned, `redact_payload` never redacts them, and the
    # last-gate defense can only check S5-identified spans). So we serialize with the
    # SAME `_collect_text` walk and land ONLY the scanned string leaves — never a
    # `json.dumps` of the raw blob, which would smuggle an entity in a KEY or a numeric
    # leaf past every tripwire into the global index (D17/D58a cross-tenant leak).
    leaves: dict[str, str] = {}
    _collect_text("related_terms", payload.get("related_terms"), leaves)
    _collect_text("structured", payload.get("structured"), leaves)
    scanned = [leaves[key] for key in sorted(leaves)]
    if scanned:
        parts.append(" ".join(scanned))
    text = "\n".join(parts)

    scope = payload.get("scope")
    title = scope if isinstance(scope, str) and scope.strip() else None

    return KnowledgeSeed(
        id=id,
        text=text,
        doc_id=env.candidate_id,
        title=title,
        status="validated",
        # Drift is not applicable to knowledge recall (only blueprints replay), but
        # the field is threaded for parity + so a retraction can stamp it.
        drift_status=env.drift.status,
        # Provenance (UI Slice 2): a loop-landed knowledge chunk is distinguishable
        # from a hand-authored fixture (`created_by="seed"`) and carries its
        # originating candidate id for incident response.
        created_by="learning",
        source_candidate_id=env.candidate_id,
        # Governed-corpus trust partition (Phase 2, SAFETY-CRITICAL): a loop-landed
        # knowledge chunk lands in the LEARNING STAGING tier, NOT the trusted MCP
        # canon. `source="learning"` keeps it OUT of knowledge recall (the
        # `source='mcp'` trust gate) and out of the MCP-scoped corpus GC;
        # `verified` defaults False (safe); a human-approve landing threads True
        # (Phase-3 triage). Without this the `KnowledgeSeed` default (`mcp`/`True`)
        # would leak an unvetted chunk into the trusted recall partition.
        source="learning",
        verified=verified,
    )


__all__ = [
    "blueprint_from_generalization",
    "blueprint_seed_from_candidate",
    "knowledge_seed_from_candidate",
]
