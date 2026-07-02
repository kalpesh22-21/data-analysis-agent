"""Retrieval data model — the frozen dataclasses the pipeline produces (D7/D8).

Per `docs/decisions/retrieval-pipeline-design.md` §3.2. Every type here is an
immutable value object: recall produces `Candidate`s; the pipeline cuts them to
`ThinCard`/`KnowledgeHit`; `RetrievedContext` is the single per-turn block the
renderer turns into one model-facing system message.

`Candidate.score` is filled AFTER construction (by rerank, or by recall
similarity on the degrade path) via `dataclasses.replace` — the dataclass is
frozen, so scoring is functional (a new value), never a mutation. `payload`
being a `dict` does not make `Candidate` unhashable-in-practice-a-problem: no
code ever hashes a `Candidate`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CandidateKind = Literal["blueprint", "knowledge"]


@dataclass(frozen=True)
class Candidate:
    """One recalled corpus item (blueprint intent or knowledge chunk)."""

    id: str
    kind: CandidateKind
    text: str  # intent (blueprint) | chunk (knowledge) — the rerank/embed text
    uses: frozenset[str] | None  # blueprint transitive "db.table.column" set; None for knowledge
    payload: dict[str, Any] = field(default_factory=dict)
    score: float | None = None  # filled by rerank (or recall similarity on degrade)


@dataclass(frozen=True)
class ThinCard:
    """A top-N blueprint thin card (03 §Thin cards) — id + intent + slots summary."""

    id: str
    intent: str
    slots_summary: str
    score: float


@dataclass(frozen=True)
class KnowledgeHit:
    """A top-N global-knowledge hit — the reranked chunk (OQ-R3 interim)."""

    id: str
    text: str
    score: float
    title: str | None = None


@dataclass(frozen=True)
class BlueprintDetail:
    """The stored D87 retrieval projection of one blueprint — what `getBlueprint`
    returns (read-tools-design §1.2), GROWN ADDITIVELY with the full DAG
    (`sql_template`, typed `slots`, `resolves`, `uses_rules`, `composes`,
    `result_grain`) now that the `runBlueprint` brick lands (runblueprint-design
    §1 / OQ-T1). The additive fields default to `None`/`empty` so every existing
    D87/D88 construction still holds — the recall projection is unchanged.

    `uses` is a `frozenset[str] | None` (NOT the rendered list): `None` is the
    fail-closed undetermined marker (a corrupt/absent stored `uses`), so a
    scope check via `scope_filter.is_blueprint_in_scope` DROPS it rather than
    fail-open — mirroring `Candidate.uses`. The tool renders it as a sorted list.

    The full-DAG fields are carried JSON-DECODED (dicts/lists/str/None), NOT the
    typed `runtime.blueprint` objects — retrieval stays free of blueprint typing;
    `runtime.blueprint.models.Blueprint.parse(...)` turns them into typed objects.
    """

    id: str
    intent: str
    slots_summary: str
    uses: frozenset[str] | None
    status: str
    drift_status: str
    hit_count: int
    catalog_sha: str
    # --- additive, the runBlueprint brick (OQ-T1) — JSON-decoded, default absent ---
    resolves: dict[str, Any] | None = None
    slots: list[dict[str, Any]] | None = None
    uses_rules: list[Any] | None = None
    sql_template: str | None = None
    composes: list[dict[str, Any]] | None = None
    result_grain: list[str] | dict[str, Any] | None = None


@dataclass(frozen=True)
class UserMemoryItem:
    """One user-memory pre-injection item (personal, entity-bearing OK)."""

    kind: str
    text: str


@dataclass(frozen=True)
class RetrievedContext:
    """The per-turn retrieval block — a deterministic function of
    (question, scope, corpus snapshot), never persisted (design §6)."""

    thin_cards: list[ThinCard] = field(default_factory=list)
    knowledge_hits: list[KnowledgeHit] = field(default_factory=list)
    user_memory: list[UserMemoryItem] = field(default_factory=list)
    # L3: a SINGLE turn-wide flag — True only when every non-empty corpus was
    # reranked. It intentionally collapses per-corpus outcomes (e.g. blueprints
    # reranked but knowledge degraded to recall order): the per-corpus truth
    # lives in the RERANKER spans (`reranker.reranked` per corpus, design §3.5),
    # not here. `False` whenever any corpus fell back to recall order.
    reranked: bool = False

    def is_empty(self) -> bool:
        """True when there is nothing to pre-inject (all corpora empty)."""
        return not (self.thin_cards or self.knowledge_hits or self.user_memory)

    @classmethod
    def empty(cls) -> RetrievedContext:
        """The empty block returned on the degrade paths (design §2)."""
        return cls(thin_cards=[], knowledge_hits=[], user_memory=[], reranked=False)


__all__ = [
    "BlueprintDetail",
    "Candidate",
    "CandidateKind",
    "KnowledgeHit",
    "RetrievedContext",
    "ThinCard",
    "UserMemoryItem",
]
