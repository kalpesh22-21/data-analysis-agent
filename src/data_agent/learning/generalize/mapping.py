"""S3 PLAN + S4 `BlueprintGeneralization` → runtime `Blueprint` / `BlueprintSeed`.

The whole point of Contract A: a validated candidate promotes (Slice 9) 1:1 onto
`runtime/blueprint/models.py::Blueprint` with ZERO field invention. This module is
that mapping — proving the round-trip in tests and reused by S9 at promotion:

  id ← minted (caller) · intent/resolves/slots ← S3 PLAN ·
  uses_rules/sql_template/result_grain ← S4 generalization ·
  composes ← S3 `ComposeNodePlan` ⨝ S4 `NodeTemplate.sql_template` (by `order`).

`blueprint_from_generalization` builds the runtime `Blueprint` (executed live).
`blueprint_seed_from_candidate` builds the parallel `BlueprintSeed` the S9 landing
writer MERGE-upserts into the neo4j retrieval corpus so a validated blueprint becomes
RECALLABLE (S9-activation Slice 2, §3.2). Both share the SAME slot + composes
projection helpers so the landed seed and the executable blueprint can never drift.
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
    """Project the S3 `parameterization` entries onto the `slots_json` shape — every
    `role=slot` param's `slot` dict (name/type/binds_to/required/…). The SINGLE slot
    projection reused by both the runtime `Blueprint` and the landed `BlueprintSeed`.

    Reads the raw plan defensively, in the same shape as `promotion/replay.py::
    _slot_types`: a non-list `parameterization` is not iterable and a non-dict entry
    has no `.get`, and this module's documented failure mode is `BlueprintParseError`
    (fail-closed landing), never a `TypeError`/`AttributeError`. A skipped malformed
    entry cannot smuggle anything through: the slot it would have declared is then
    missing, so its `{token}` is an undeclared placeholder that the corpus loader
    refuses at the landing write."""
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
    """Project the S3 `composes` DAG ⨝ the S4 per-node `sql_template` (by `order`)
    onto the `composes_json` shape. The SINGLE composes projection reused by both the
    runtime `Blueprint` and the landed `BlueprintSeed`.

    Only `_NODE_DOC_FIELDS` are carried. Two PLAN fields are deliberately dropped:

      * `source_tool_call_ref` — a SESSION-scoped identifier (D17). The corpus holds
        entity-free, session-independent artifacts; session linkage belongs in the
        `evidence_ref`s in the access-controlled `learning_audit` store, not in a
        globally-recallable `:Blueprint` property.
      * `step_intent` — free NL, and (unlike `intent`/`notes`) NOT one of the S5
        `_ENTITY_FREE_SURFACES` the leakage gate scans. Landing it would put
        UNSCANNED model prose into the global corpus, and because the last-gate
        `_assert_seed_entity_free` can only match spans S5 already identified, the
        tripwire could not catch it either. Dropped until it is either scanned or
        proven unnecessary — the runtime never reads it, so nothing regresses.
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

    Raises `BlueprintParseError` (from `Blueprint.parse`) if any mapped field is
    malformed — the round-trip contract (`S4-payload-maps-to-runtime-blueprint`)
    asserts this succeeds with no missing field for a valid candidate.

    `window_anchor` (J7/J7c) is read from the S3 PLAN like `intent`/`resolves`, and is
    INERT until the extractor learns to declare it: today no candidate carries the key,
    so `payload.get` yields `None` = "no claim" and every landing is byte-identical to
    before. It is threaded anyway because the alternative failure is silent — the first
    learned WINDOWED blueprint would otherwise promote to canon anchor-less, and J7 (the
    model re-deriving a data-anchored window with its own calendar SQL) would return for
    the learned corpus with nothing failing anywhere. Validation is `Blueprint.parse`'s
    closed-set check, so a malformed declaration is a fail-closed `BlueprintParseError`
    at landing, never a dropped field.
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

    Reuses `blueprint_from_generalization` to VALIDATE + normalize the structure (the
    same parse the executable blueprint takes), then projects the generalized,
    entity-free fields onto the seed the landing writer MERGE-upserts. Reads ONLY
    generalized fields (template / resolves / slots / result_grain / intent / uses) —
    never `evidence`, audit spans, or any entity-bearing payload (D17); the writer
    additionally asserts the seed is entity-free before any neo4j write.

    `id` is the deterministic landing id (derived from the canonical_key by the
    caller, `promotion/landing.py::landing_id`) so a re-promotion MERGEs in place.

    `verified` is the Phase-3 human-approval flag threaded onto the seed: an auto-landed
    node is `verified=False` (the safe default), a human-approved landing is
    `verified=True` (the `apply_human_decision` approve path passes it through). `source`
    stays `"learning"` regardless — verification does not move the node into the trusted
    MCP canon partition (that is the Phase-3 promote → manual-PR reseed).

    Raises `ValueError` when the candidate carries no `generalization` (a non-blueprint
    or malformed candidate can never land), and `BlueprintParseError` when the
    generalized structure is malformed (fail-closed — never a silent broken landing).
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
    """Project an approved global-knowledge candidate onto the neo4j-corpus
    `KnowledgeSeed` (UI Slice 2 §1.1) — the knowledge-side mirror of
    `blueprint_seed_from_candidate`. Pure function.

    Reads ONLY the entity-free knowledge surfaces the leakage gate scans
    (`leakage/gate.py::_ENTITY_FREE_SURFACES` — `statement`, `structured`,
    `related_terms`, `scope`): `text` ← `statement` (concatenated with
    `related_terms` + a serialized `structured` for richer recall), `title` ←
    `scope`, `doc_id` ← `env.candidate_id`, `id` ← the deterministic landing id,
    plus the provenance/drift fields. NEVER reads `evidence`, audit spans, or any
    entity-bearing payload (D17); the landing writer additionally asserts the seed is
    entity-free before any neo4j write.

    `id` is the deterministic landing id (`promotion/landing.py::landing_id`, the
    `kn::`-prefixed form) so a re-promotion MERGEs the same node in place. `verified`
    threads the Phase-3 human-approval flag (auto-land False, human-approve True);
    `source` stays `"learning"` either way.

    Raises `ValueError` when `statement` is empty — an empty knowledge chunk is never
    landed (it would recall nothing meaningful and only pollute the index).
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
