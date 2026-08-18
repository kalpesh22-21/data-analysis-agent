"""Contract A — the S4-enriched blueprint payload (Wave-0 contract freeze, D102).

S4 computes a `BlueprintGeneralization` and merges it under `payload["generalization"]` on a
blueprint candidate; S6 hashes it and S9 replays it. It maps 1:1 onto
`runtime/blueprint/models.py::Blueprint`, so a promoted candidate is executable with zero
translation. These are pure frozen value objects with `to_doc`/`from_doc` round-trip
fidelity — no stage logic — and the doc they serialize is entity-free by construction,
derived only from the templated SQL and never from the values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class NodeTemplate:
    """One composite node's AST-rewritten template (composite blueprints only)."""

    order: int  # matches ComposeNodePlan.order → runtime Node.order
    sql_template: str

    def to_doc(self) -> dict[str, Any]:
        return {"order": self.order, "sql_template": self.sql_template}

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> NodeTemplate:
        return cls(order=doc["order"], sql_template=doc["sql_template"])


@dataclass(frozen=True)
class ResultGrainStamp:
    """The D56 grain teeth → runtime `ResultGrain` (columns + verifiable)."""

    columns: tuple[str, ...] = ()
    verifiable: bool = True

    def to_doc(self) -> dict[str, Any]:
        return {"columns": list(self.columns), "verifiable": self.verifiable}

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> ResultGrainStamp:
        return cls(
            columns=tuple(doc.get("columns", []) or []),
            verifiable=bool(doc.get("verifiable", True)),
        )


@dataclass(frozen=True)
class StaticValidation:
    """The S4 dry-run stamp.

    `outcome=="fail_to_review"` is an in-band value S7 routes on — an un-rewritable candidate is
    reviewed, never dropped (D52/D97).
    """

    explain_ok: bool  # explainQuery dry-run parsed vs. current schema
    binds_to_subset_uses: bool  # every slot.binds_to ∈ uses (corpus_loader assertion)
    dag_ok: bool  # composite: feeds_from/cycles/cap/terminal-approval/scalar-converge
    read_only_select: bool  # single read-only SELECT; no '*', no dict-family funcs (D52)
    outcome: Literal["ok", "fail_to_review"]  # ANY false above ⇒ fail_to_review
    reason: str | None = None  # stable machine tag for the first failing check

    def to_doc(self) -> dict[str, Any]:
        return {
            "explain_ok": self.explain_ok,
            "binds_to_subset_uses": self.binds_to_subset_uses,
            "dag_ok": self.dag_ok,
            "read_only_select": self.read_only_select,
            "outcome": self.outcome,
            "reason": self.reason,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> StaticValidation:
        return cls(
            explain_ok=bool(doc["explain_ok"]),
            binds_to_subset_uses=bool(doc["binds_to_subset_uses"]),
            dag_ok=bool(doc["dag_ok"]),
            read_only_select=bool(doc["read_only_select"]),
            outcome=doc["outcome"],
            reason=doc.get("reason"),
        )


@dataclass(frozen=True)
class BlueprintGeneralization:
    """What S4 computes and merges under `payload["generalization"]`.

    Entity-free by construction: derived only from the templated SQL, never the values.
    """

    sql_template: str | None  # single: top-level template. composite: None (per-node below)
    uses: tuple[str, ...]  # BYTE-EXACT "database.table.column" scope keys (D87)
    uses_rules: tuple[str, ...]  # resolved catalog rule ids for role=rule locators (D48 input)
    node_templates: tuple[NodeTemplate, ...]  # composite only: one per composes[*].order
    result_grain: ResultGrainStamp  # the D56 teeth (from result_signature.grain)
    static_validation: StaticValidation  # the dry-run stamp; gates promotion-eligibility
    canonical_ast_norm: str  # sqlglot-normalized template text — the S6 hash input (D48)

    def to_doc(self) -> dict[str, Any]:
        return {
            "sql_template": self.sql_template,
            "uses": list(self.uses),
            "uses_rules": list(self.uses_rules),
            "node_templates": [n.to_doc() for n in self.node_templates],
            "result_grain": self.result_grain.to_doc(),
            "static_validation": self.static_validation.to_doc(),
            "canonical_ast_norm": self.canonical_ast_norm,
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> BlueprintGeneralization:
        return cls(
            sql_template=doc.get("sql_template"),
            uses=tuple(doc.get("uses", []) or []),
            uses_rules=tuple(doc.get("uses_rules", []) or []),
            node_templates=tuple(
                NodeTemplate.from_doc(n) for n in doc.get("node_templates", []) or []
            ),
            result_grain=ResultGrainStamp.from_doc(doc.get("result_grain", {}) or {}),
            static_validation=StaticValidation.from_doc(doc["static_validation"]),
            canonical_ast_norm=doc.get("canonical_ast_norm", ""),
        )
