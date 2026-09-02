"""Extractor output models — the shared candidate header + the `blueprint` payload.

The extractor emits a PLAN, never SQL (D35): `parameterization` classifies each literal
predicate of the ACCEPTED SQL as `slot | rule | inline` (totality, no drop — D97), and the
deterministic AST rewrite is S4. Evidence is MANDATORY (D31) — a zero-evidence candidate is
rejected at emit. `EvidenceRef.quote` is entity-bearing and is snapshotted to
`learning_audit` (D51/D95); the persisted candidate carries only the minted `evidence_ref`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# THE definitions, imported DOWNWARD (D58c permits learning -> runtime; it forbids
# only runtime -> learning) and RE-EXPORTED under their historical names here so the
# extractor package's own importers are unaffected. Stage-4 maps them 1:1 onto
# SlotSpec/Node.
#
# These used to be hand-kept MIRRORS, on the rationale that the extractor package
# should not import the request-path blueprint module — a rationale that had already
# lapsed (`extractor/validation.py` imports `runtime.blueprint.template`, and
# `generalize/canonical.py` imports `runtime.blueprint.structural_key`) and that the
# mirror did not survive anyway. `SLOT_TYPES` shipped without
# `relative_window`/`period_range` (D41/D49) and the gap was invisible for two reasons
# at once: `schema.py::SLOT_TYPE_ENUM` is derived from this set, so the tool schema
# never OFFERED the correct type, and a compliant model therefore reached for
# `period`/`string` instead — which extract CLEANLY. Three of the ten canon blueprints
# (`bp-hires-per-month`, `bp-hires-projection`, `bp-hires-in-range`) were un-relearnable
# as a result. A parity test caught it after the fact; the import makes the drift
# unrepresentable. `tests/learning/extractor/test_slot_type_mirror_drift_qa.py` now
# asserts IDENTITY with the runtime and stays as the tripwire against re-mirroring.
from data_agent.runtime.blueprint.models import NODE_KINDS as NODE_KINDS
from data_agent.runtime.blueprint.models import SLOT_TYPES as SLOT_TYPES

# The acceptance type is S2's — it is the summary loader that DERIVES the signal and the
# extractor that consumes it — so it is defined next to the producer and re-exported here
# under its historical name. The direction is forced: this package already imports
# `..summary.models` (extractor/validation.py, extractor/prior_art.py), so summary
# importing back would cycle.
from ..summary.models import AcceptedSignal as AcceptedSignal

CandidateType = Literal["blueprint", "global_knowledge", "user_knowledge", "schema_edit"]
Role = Literal["slot", "rule", "inline"]

# The two WINDOWED types, and the ONE thing that makes them structurally different
# from every other slot: they consume no warehouse column DOMAIN, so
# `runtime/blueprint/models.py::SlotSpec.parse` REFUSES a `binds_to` on them ("a
# windowed-period slot consumes no domain — it would fire a useless probe"). That is
# a landing-time raise, several stages downstream of here, so the rule is mirrored
# and enforced at extraction (`validation.py::_validate_roles`) where it becomes a
# traceable Decline instead of a `BlueprintParseError` out of the promotion path.
#
# This is the reason widening `SLOT_TYPES` alone is NOT sufficient: the tool schema
# marks `binds_to` required for every slot, so a model that finally CAN say
# `relative_window` would attach a `binds_to` to it and land nothing.
WINDOWED_SLOT_TYPES: frozenset[str] = frozenset({"relative_window", "period_range"})

# Types the runtime EXECUTES but this pipeline cannot yet PRODUCE. Withheld from the
# prompt enum (`schema.py::SLOT_TYPE_ENUM`) and declined at validation.
#
# `period_range` is the only member, and the reason is a WRITER, not a reader:
# `generalize/rewrite.py::rewrite_sql_to_template` stamps exactly ONE placeholder per
# matched literal, named after the slot (`literal.replace(exp.Placeholder(this=name))`).
# The runtime's date-range grammar is TWO tokens — `{name}_start` and `{name}_end`
# (`runtime/blueprint/slots.py::slot_token_names`) — and nothing in the extractor or
# generalize packages knows that grammar exists (grep `_start` across both: no hits).
# So both shapes a model can emit dead-end at landing:
#
#   one slot, two predicates → two `{name}` tokens for a slot whose legal tokens are
#     `{name}_start`/`{name}_end`, plus a duplicated slot declaration
#     ⇒ BlueprintParseError: duplicate slot name
#   two slots each typed period_range → each mints `{n}_start_start`, `{n}_start_end`…
#     ⇒ CorpusLoadError: references undeclared slot(s)
#
# And — the reason this is WITHHELD rather than merely documented — golden replay
# reports `passed=True` for both, because the fake probe never executes the SQL. A
# silent dead end at replay is strictly worse than an honest decline at extraction; it
# is the same argument that made `binds_to` type-dependent rather than merely offered.
#
# TO RE-ENABLE this is NOT a suffix in the rewriter. `parameterization` is one entry per
# literal predicate, each carrying its own `slot`, so there is no way to say "these two
# predicates are the two BOUNDS of one range". That needs a payload shape, a rewriter
# change, and a rule deciding WHICH bound each predicate is — and getting that last one
# wrong silently inverts a date filter, which is the D56 wrong-answer class. Tracked in
# the plan doc; pinned by `test_period_range_withdrawn_qa.py`.
UNSUPPORTED_SLOT_TYPES: frozenset[str] = frozenset({"period_range"})

@dataclass(frozen=True)
class EvidenceRef:
    turn_ref: int  # SessionSummary turn index
    tool_call_ref: str  # tool_call_id in the D46 tool trail
    quote: str  # snapshotted to learning_audit; NOT stored on the candidate

    def to_doc(self) -> dict[str, Any]:
        return {"turn_ref": self.turn_ref, "tool_call_ref": self.tool_call_ref, "quote": self.quote}


@dataclass(frozen=True)
class EntitySelfCheck:
    contains_entities: bool  # preliminary; the Slice-5 leakage gate is authoritative
    found: tuple[str, ...] = ()

    def to_doc(self) -> dict[str, Any]:
        return {"contains_entities": self.contains_entities, "found": list(self.found)}


@dataclass(frozen=True)
class CandidateHeader:
    type: CandidateType
    confidence: float  # extractor self-assessment, 0.0–1.0
    evidence: tuple[EvidenceRef, ...]  # MANDATORY, non-empty — else REJECTED (D31)
    rationale: str  # why worth learning AND why generic / not already stored
    proposed_action: str  # "new" | "update_existing:<id>" | "reinforce:<id>" — HINT only
    entity_self_check: EntitySelfCheck
    depends_on: tuple[str, ...] = ()  # sibling candidate ids this one is blocked on (§7)


# --- blueprint payload (§3) — the depth target ---------------------------------


@dataclass(frozen=True)
class Locator:
    table: str  # "database.table"
    column: str
    value: str  # the literal as it appeared (pre-generalization)

    def to_doc(self) -> dict[str, Any]:
        return {"table": self.table, "column": self.column, "value": self.value}


@dataclass(frozen=True)
class SlotPlan:
    name: str
    type: str  # ∈ SLOT_TYPES
    # "database.table.column" — Stage-4 asserts ⊆ uses. `None` ONLY for a
    # `WINDOWED_SLOT_TYPES` slot, which consumes no column domain and which the
    # runtime `SlotSpec.parse` refuses to accept a `binds_to` for.
    #
    # `None` means ABSENT, and `""` is NOT a synonym for it here — `SlotSpec.parse`
    # tests `binds_to is not None`, so an empty string is a value it REFUSES, and
    # `_validate_roles` mirrors that exact test rather than a truthiness one. A
    # surviving `None` on a windowed slot is therefore a deliberate declaration that
    # passed an `is not None` gate, not merely a falsy field.
    binds_to: str | None
    required: bool
    optional_pattern: str | None = None  # SQL fragment when an optional slot is ABSENT
    enum_values: tuple[str, ...] | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "binds_to": self.binds_to,
            "required": self.required,
            "optional_pattern": self.optional_pattern,
            "enum_values": list(self.enum_values) if self.enum_values is not None else None,
        }


@dataclass(frozen=True)
class ParamPlan:
    locator: Locator
    role: Role
    slot: SlotPlan | None = None  # role=slot
    rule_id: str | None = None  # role=rule → an EXISTING catalog rule
    why: str | None = None  # role=inline → why it is structural / metric-defining

    def to_doc(self) -> dict[str, Any]:
        return {
            "locator": self.locator.to_doc(),
            "role": self.role,
            "slot": self.slot.to_doc() if self.slot is not None else None,
            "rule_id": self.rule_id,
            "why": self.why,
        }


@dataclass(frozen=True)
class ColumnShape:
    column: str
    type: str

    def to_doc(self) -> dict[str, Any]:
        return {"column": self.column, "type": self.type}


@dataclass(frozen=True)
class ResultGrainPlan:
    columns: tuple[str, ...] = ()
    verifiable: bool = True

    def to_doc(self) -> dict[str, Any]:
        return {"columns": list(self.columns), "verifiable": self.verifiable}


@dataclass(frozen=True)
class ResultSignature:
    shape: tuple[ColumnShape, ...]
    grain: ResultGrainPlan
    invariants: tuple[str, ...]

    def to_doc(self) -> dict[str, Any]:
        return {
            "shape": [s.to_doc() for s in self.shape],
            "grain": self.grain.to_doc(),
            "invariants": list(self.invariants),
        }


@dataclass(frozen=True)
class ComposeNodePlan:
    order: int
    node_kind: str  # ∈ NODE_KINDS
    step_intent: str
    feeds_from: tuple[int, ...] = ()
    consumes: dict[str, str] = field(default_factory=dict)
    output: dict[str, str] = field(default_factory=dict)
    source_tool_call_ref: str | None = None
    when: str | None = None
    requires_approval: dict[str, Any] | None = None

    def to_doc(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "node_kind": self.node_kind,
            "step_intent": self.step_intent,
            "feeds_from": list(self.feeds_from),
            "consumes": dict(self.consumes),
            "output": dict(self.output),
            "source_tool_call_ref": self.source_tool_call_ref,
            "when": self.when,
            "requires_approval": self.requires_approval,
        }


@dataclass(frozen=True)
class BlueprintPayload:
    intent: str  # NL, ENTITY-FREE, embedded for retrieval
    kind: str  # "single" | "composite"
    resolves: dict[str, str]  # {ambiguous_term: column_chosen}
    source_tool_call_refs: tuple[str, ...]  # runQuery refs that produced the ACCEPTED answer (D34)
    accepted_signal: str  # MANDATORY (D34)
    parameterization: tuple[ParamPlan, ...]  # one entry per literal predicate (§4)
    composes: tuple[ComposeNodePlan, ...] = ()  # kind=composite only
    result_signature: ResultSignature | None = None
    notes: str = ""
    # ⚠ THERE IS DELIBERATELY NO `sql_rewrite` FIELD HERE, and its absence is load-bearing.
    #
    # §C.5's rewrite badge (`payload["sql_rewrite"]`) is an attestation about PROVENANCE — "the
    # assistant wrote this query, not the session". Registering it here would make it a field a
    # MODEL can set, because everything in this class is rehydrated from model-authored JSON: a
    # mined candidate could then render "the assistant rewrote this SQL" on a review card while
    # `authored=False` let it auto-land, a claim and a routing decision disagreeing about one
    # row. The first cut of this slice did register it, which is exactly how that was found.
    #
    # So the badge is stamped by `inbox/completion.py::_stamped` onto the validated DOC, after
    # `to_candidate` has run, from a record derived from the `ValidationSnapshot`. `to_doc`
    # being a CLOSED key set is then the mechanism rather than the obstacle: it is what drops an
    # inbound claim on the way through.

    def to_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "intent": self.intent,
            "kind": self.kind,
            "resolves": dict(self.resolves),
            "source_tool_call_refs": list(self.source_tool_call_refs),
            "accepted_signal": self.accepted_signal,
            "parameterization": [p.to_doc() for p in self.parameterization],
            "composes": [c.to_doc() for c in self.composes],
            "result_signature": (
                self.result_signature.to_doc() if self.result_signature is not None else None
            ),
            "notes": self.notes,
        }
        return doc


# --- the extractor's in-flight output ------------------------------------------


@dataclass(frozen=True)
class ExtractedCandidate:
    """One structurally-valid candidate the extractor emitted (evidence present).

    `payload` is a `BlueprintPayload` for `type=="blueprint"`, else a validated dict (the other
    three targets are minimally modelled in S3).
    """

    header: CandidateHeader
    payload: BlueprintPayload | dict[str, Any]

    def payload_to_doc(self) -> dict[str, Any]:
        if isinstance(self.payload, BlueprintPayload):
            return self.payload.to_doc()
        return dict(self.payload)


@dataclass(frozen=True)
class Decline:
    """A candidate the extractor rejected before emit, with the reason code the consumer traces.

    MOSTLY not persisted: a decline whose reason says the candidate was WRONG (no evidence, no
    acceptance, un-parseable SQL) dies here. The one exception is the narrow class saying the
    FORM COULD NOT BE FILLED IN, which `consumer.py` routes to the candidate store for a human
    to complete.

    `correctable` splits the two: a CORRECTABLE decline is one whose fix the extractor can NAME,
    so the sentence may be put in front of the model; everything else judges CONTENT with no fix
    to hand. Only `validation.py::_correctable` sets the flag.
    `corrections_attempted`/`correction_history` record what was actually tried, so "the model
    could not produce a valid candidate" stays distinguishable from "the model was never asked
    twice"; the history carries whatever those declines' details carried (see
    `consumer.py::ENTITY_BEARING_DECLINE_REASONS`).
    """

    type: str
    reason: str
    detail: str = ""
    correctable: bool = False
    corrections_attempted: int = 0
    correction_history: tuple[str, ...] = ()
    # The RAW candidate envelope this decline was judged on, verbatim, as
    # `parse_candidates` read it out of the tool call.
    #
    # Present ONLY for a decline a CORRECTION TOUCHED, which is two cases, and WHICH
    # attempt it holds differs between them:
    #   - it SURVIVED the correction — re-emitted and declined again, or never
    #     re-answered — and the payload is the model's LAST attempt, taken by index from
    #     the array it last emitted (`extractor.py::_finish`);
    #   - the model WITHDREW it, taking the sanctioned "omit what you cannot fix" exit,
    #     so there IS no last attempt: the payload is the attempt the correction NAMED,
    #     taken from the array that correction pointed at
    #     (`extractor.py::_withdrawn_declines`, resolved at withdrawal time because one
    #     correction later that array is gone).
    # Every other decline carries `None`, including a substantive one from the same
    # batch, because a candidate that was never re-asked has no attempt distinct from
    # its first and nothing downstream may treat the two as interchangeable.
    #
    # UNVALIDATED BY CONSTRUCTION: it is exactly the JSON that FAILED validation, so
    # every reader normalizes rather than trusts (`candidate/models.py::
    # build_declined_envelope` is the only one today). It is kept whole rather than
    # narrowed to `payload` because the fields outside it — confidence, proposed_action,
    # depends_on — are what a review item needs to render as a candidate rather than as
    # a fragment.
    raw_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExtractionResult:
    candidates: tuple[ExtractedCandidate, ...]
    # ORDER IS PART OF THE CONTRACT: `(*settled, *withdrawn, *corrected)`. It matters
    # because `consumer.py::_persist_declined_for_review` takes `eligible[-1]` for the
    # single review slot, so a decline that SURVIVED correction beats one the model
    # withdrew — deliberately, since the survivor's `raw_payload` is the model's latest
    # attempt while the withdrawn one's is the attempt it gave up on.
    declines: tuple[Decline, ...] = ()
    # Corrective turns SPENT on this extraction (0 for the overwhelming majority).
    # Surfaced on the extract span because it is a prompt-quality signal, not a model
    # one: a rate that climbs means the tool schema and the system prompt are asking
    # for something the model keeps mis-packaging, and that is fixable at the prompt.
    corrections: int = 0
