"""Retrieval data model — the frozen dataclasses the pipeline produces (D7/D8).

Every type here is an immutable value object: recall produces `Candidate`s; the pipeline
cuts them to `ThinCard`/`KnowledgeHit`; `RetrievedContext` is the single per-turn block the
renderer turns into one model-facing message.

`Candidate.score` is filled AFTER construction — by rerank, or by recall similarity on the
degrade path — via `dataclasses.replace`: the dataclass is frozen, so scoring is functional,
never a mutation.
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
class SlotSummary:
    """One slot as it appears on a blueprint SEARCH card.

        DELIBERATELY `{name, type, required}` and nothing else — enough to judge applicability
        and to fill `runBlueprint`. The AUTHORED slot (`slots_json`, `blueprint.models.SlotSpec`)
        additionally carries `binds_to` ("database.table.column"), `enum_values`,
        `optional_pattern` and numeric bounds: those are execution detail `getBlueprint` exists
        to serve, and `binds_to` in particular is a fully-qualified COLUMN IDENTIFIER that would
        otherwise ship on every one of `k` cards.

        The projection is enforced in exactly ONE place, `RetrievalPipeline._to_thin_card`;
        `Candidate.payload` may carry the raw decoded slots, the card may not. Widening this type
        widens both the pre-injected block and the tool result at once.
    """

    name: str
    type: str
    required: bool


def coerce_result_grain(raw: Any) -> tuple[str, ...] | None:
    """Coerce a stored `result_grain` into the card's column tuple, else `None`.

        A grain is legal in two shapes: a bare list of column/alias names, or
        `{"columns": [...], "verifiable": bool}`. The card carries the COLUMN TUPLE only —
        `verifiable` is a D56 statement about the runtime's own post-run checking, not something
        the model routes on. Anything else — a non-list, a dict with no usable `columns`, a list
        with no strings — degrades to `None` and the key is omitted (fail-soft, never raises).
    """
    columns: Any = raw.get("columns") if isinstance(raw, dict) else raw
    if not isinstance(columns, list):
        return None
    names = tuple(c for c in columns if isinstance(c, str) and c)
    return names or None


@dataclass(frozen=True)
class ThinCard:
    """A top-N blueprint thin card — id + intent + slots summary, ENRICHED so the model can
        choose between candidates without spending a `getBlueprint` round-trip per candidate.

        The additive fields default to `None`/`0`, so every existing construction site stays
        valid AND a blueprint with no stored DAG produces a card that renders and serialises
        byte-identically to before.

        `status` is DELIBERATELY ABSENT. Recall filters
        `coalesce(node.status,'validated') = 'validated'`, so every card is validated BY
        CONSTRUCTION — the field would be a constant carrying no information, and surfacing it
        would imply a distinction that cannot occur in a search result. It stays on
        `getBlueprint`, where a KEYED fetch by id genuinely can return a non-validated blueprint.
    """

    id: str
    intent: str
    slots_summary: str
    score: float
    # --- additive (release-1 §02) — the routing enrichment ------------------
    resolves: dict[str, str] | None = None  # pinned term → column NAME
    slots: tuple[SlotSummary, ...] | None = None  # projected + capped, see below
    result_grain: tuple[str, ...] | None = None
    # How many slots the per-card cap dropped (`pipeline._MAX_CARD_SLOTS`). The
    # count travels ON the card because neither the renderer nor the tool can
    # re-derive it from a truncated tuple, and without it a 12-slot blueprint
    # would silently claim to have 6 — the model would then under-fill
    # `runBlueprint`. Rendered as the `(+K more)` marker.
    slots_omitted: int = 0
    # NOT model-facing and never rendered/serialised: the blueprint's transitive
    # `uses` footprint, carried so `SearchBlueprintsTool` can set the trail
    # entry's D44 provenance to the union of the returned cards' footprints
    # (release-1 §02 ⚠Provenance). `None` is the fail-closed undetermined
    # marker, mirroring `Candidate.uses`.
    uses: frozenset[str] | None = None


@dataclass(frozen=True)
class KnowledgeHit:
    """A top-N global-knowledge hit — the reranked chunk (OQ-R3 interim)."""

    id: str
    text: str
    score: float
    title: str | None = None


@dataclass(frozen=True)
class BlueprintDetail:
    """The stored retrieval projection of one blueprint — what `getBlueprint` returns, GROWN
        ADDITIVELY with the full DAG (`sql_template`, typed `slots`, `resolves`, `uses_rules`,
        `composes`, `result_grain`). The additive fields default to `None`/empty, so the recall
        projection is unchanged.

        `uses` is a `frozenset[str] | None`, NOT the rendered list: `None` is the fail-closed
        undetermined marker (a corrupt or absent stored `uses`), so a scope check DROPS it rather
        than fail open, mirroring `Candidate.uses`. The tool renders it as a sorted list.

        The full-DAG fields are carried JSON-DECODED, not as typed `runtime.blueprint` objects —
        retrieval stays free of blueprint typing; `Blueprint.parse` does that conversion.
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
    # J7 — the blueprint's OPTIONAL window-anchor declaration (`"data"` | `"calendar"`),
    # carried as the stored string. `None` means the blueprint declares none, which is
    # every blueprint that is not windowed and every one authored before the field
    # existed — the tool then omits it and the result shape is unchanged. Unlike the DAG
    # fields this is NOT JSON: a closed enum stored as a plain neo4j property.
    window_anchor: str | None = None


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
    "SlotSummary",
    "ThinCard",
    "UserMemoryItem",
    "coerce_result_grain",
]
