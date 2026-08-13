"""Extractor output models — the shared candidate header + the `blueprint`
payload (learning-loop-extractor-design §2/§3, D31/D34/D97).

The extractor emits a PLAN, never SQL (D35): `parameterization` classifies each
literal predicate of the ACCEPTED SQL as `slot | rule | inline` (totality, no
drop — D97); the deterministic AST rewrite → `sql_template` is Slice 4. Evidence
is MANDATORY (D31): a zero-evidence candidate is rejected at emit, before it ever
reaches the audit snapshot.

`EvidenceRef.quote` is entity-bearing and is snapshotted to `learning_audit`
(D51/D95); the persisted candidate carries only the minted `evidence_ref`, never
the quote (see `audit`/`candidate` stores).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CandidateType = Literal["blueprint", "global_knowledge", "user_knowledge", "schema_edit"]
AcceptedSignal = Literal["no_correction", "thumbs_up", "explicit_confirm"]
Role = Literal["slot", "rule", "inline"]

# Mirror of blueprint/models.py SLOT_TYPES + NODE_KINDS (kept local so the
# extractor package does not import the request-path blueprint module; Stage-4
# maps these 1:1 onto SlotSpec/Node).
#
# PARITY IS ENFORCED, not remembered: `tests/learning/extractor/
# test_slot_type_mirror_drift_qa.py` asserts set-equality with the runtime for BOTH
# mirrors. It has to be, because this one drifted. The mirror shipped without
# `relative_window`/`period_range` (D41/D49) and the gap was invisible for two
# reasons at once: `schema.py::SLOT_TYPE_ENUM` is derived from this set, so the tool
# schema never OFFERED the correct type, and a compliant model therefore reached for
# `period`/`string` instead — which extract CLEANLY. Three of the ten canon
# blueprints (`bp-hires-per-month`, `bp-hires-projection`, `bp-hires-in-range`) were
# un-relearnable as a result. The parity test is the only thing that makes a mirror
# safe; add one with the mirror, never after it.
SLOT_TYPES: frozenset[str] = frozenset(
    {"string", "entity", "enum", "period", "as_of_date", "list",
     "relative_window", "period_range"}
)

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

NODE_KINDS: frozenset[str] = frozenset({"query", "approval"})


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

    def to_doc(self) -> dict[str, Any]:
        return {
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


# --- the extractor's in-flight output ------------------------------------------


@dataclass(frozen=True)
class ExtractedCandidate:
    """One structurally-valid candidate the extractor emitted (evidence present).
    `payload` is a `BlueprintPayload` for `type=="blueprint"`, else a validated
    dict (the other three targets are minimally modelled in S3 — §3.3)."""

    header: CandidateHeader
    payload: BlueprintPayload | dict[str, Any]

    def payload_to_doc(self) -> dict[str, Any]:
        if isinstance(self.payload, BlueprintPayload):
            return self.payload.to_doc()
        return dict(self.payload)


@dataclass(frozen=True)
class Decline:
    """A candidate the extractor rejected before emit (never persisted), with the
    reason code the consumer traces (fail-to-review / no-evidence / totality / …).

    `correctable` splits the two kinds apart. A CORRECTABLE decline is one whose fix the
    extractor can NAME: a reader failed and `detail` says which field and what shape, or
    a catalog check failed and `detail` says which id or which predicate. The extractor
    may put that sentence in front of the model and let it re-emit. Everything else is a
    rule about CONTENT judging a candidate it read successfully with no fix to hand, and
    re-asking is talking a model into a candidate it was right to decline. Only
    `validation.py::_correctable` sets the flag; see it for where the line sits and why.

    `corrections_attempted` / `correction_history` are the record of what was actually
    tried, and they exist so "the model could not produce a valid candidate" is
    distinguishable from "the model was never asked twice" — a zero here on a
    correctable decline means the budget was spent or disabled, not that the model
    refused. `correction_history` carries whatever the DETAIL of those declines carried:
    entity-free for the shape family, and for the two hinting families a model-authored
    identifier or a literal of the accepted SQL (see `validation.py::_correctable`,
    which owns that rule, and `consumer.py::ENTITY_BEARING_DECLINE_REASONS`, which
    records which reason codes are affected)."""

    type: str
    reason: str
    detail: str = ""
    correctable: bool = False
    corrections_attempted: int = 0
    correction_history: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionResult:
    candidates: tuple[ExtractedCandidate, ...]
    declines: tuple[Decline, ...] = ()
    # Corrective turns SPENT on this extraction (0 for the overwhelming majority).
    # Surfaced on the extract span because it is a prompt-quality signal, not a model
    # one: a rate that climbs means the tool schema and the system prompt are asking
    # for something the model keeps mis-packaging, and that is fixable at the prompt.
    corrections: int = 0
