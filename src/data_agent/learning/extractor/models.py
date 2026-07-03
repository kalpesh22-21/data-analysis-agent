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
SLOT_TYPES: frozenset[str] = frozenset(
    {"string", "entity", "enum", "period", "as_of_date", "list"}
)
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
    binds_to: str  # "database.table.column" — Stage-4 asserts ⊆ uses
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
    reason code the consumer traces (fail-to-review / no-evidence / totality / …)."""

    type: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class ExtractionResult:
    candidates: tuple[ExtractedCandidate, ...]
    declines: tuple[Decline, ...] = ()
