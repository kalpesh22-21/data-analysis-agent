"""PriorArtCard — the entity-free projection the prior-art search returns.

CARDS ONLY, NEVER PAYLOADS, and two live facts force it: a candidate at `status="extracted"`
has NOT passed the leakage gate (S5 runs after generalize), and `extractor_rationale` is never
touched by `strip_entity_bearing`, which only redacts payload string leaves and blanks scan
spans. So a card carries only surfaces that are entity-free BY CONSTRUCTION or that the gate
explicitly scans, plus derived structure and lifecycle flags — no SQL, no evidence, no
rationale, no payload leaves.

It carries `result_grain`, not the plan's `result_signature`: there is no such property on a
node, and naming the card field after the RAW extractor field — the one the gate scans
precisely because it can carry entities — would invite someone to populate it from that one.

The trust TIER is a first-class field, because the search deliberately drops recall's
`source='mcp'` gate and consumers decide differently per partition — only an `mcp` match means
"the canon already has this". A node with no usable `source` maps to the explicit `unsourced`
tier rather than defaulting into either real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from data_agent.runtime.retrieval.models import CandidateKind

# The artifact kinds a prior-art search covers — the two vector-indexed neo4j corpora.
# It is not a parallel vocabulary: it is recall's `CandidateKind` under a local name,
# because prior art reads the SAME two indexes recall does (`vector_index.py::
# CORPUS_INDEX_BY_KIND`), and a kind either side knew about alone would be a lookup
# against an index that does not exist. Aliased rather than re-declared so the two can
# never drift apart. Importing `runtime.retrieval.models` costs nothing this package
# cares about — it pulls no sqlglot and no neo4j, which is the import budget
# `priorart/__init__.py` documents.
PriorArtKind = CandidateKind

# The trust partitions a card can come from.
#   mcp        — the git-versioned MCP canon. The agent ALREADY recalls this.
#   learning   — the learning staging tier (landed, but recall ignores it).
#   unsourced  — a node with no usable `source` property. Both writers always stamp one
#                (`corpus_loader._UPSERT_BLUEPRINT` / `generalize/mapping.py`), so this
#                can only be a hand edit or a foreign writer. It is surfaced rather than
#                dropped — prior art wants to know the artifact exists — but it is NEVER
#                treated as canon, so it can never trigger the drop-the-candidate verdict.
TIER_MCP = "mcp"
TIER_LEARNING = "learning"
TIER_UNSOURCED = "unsourced"
PriorArtTier = Literal["mcp", "learning", "unsourced"]

# Lifecycle states that make an artifact DEAD prior art: a human declined it, or it was
# pulled from the corpus. Excluding them is what stops a rejected candidate coming back
# as prior art forever (plan §2 prerequisite). Note the polarity is the OPPOSITE of
# recall's eligibility filter, deliberately: recall asks "is this positively eligible?"
# and fails closed, prior art asks "was this positively killed?" and fails OPEN, because
# under-including here means re-proposing something we already have an opinion about.
TERMINAL_STATUSES: frozenset[str] = frozenset({"rejected", "retired"})

# How much a cross-embedding-model cosine is discounted (§ embedding-model skew).
#
# This is NOT a tuned number and must not be read as one. The hydrator preserves
# `source='learning'` nodes across an embedding-model change WITHOUT re-embedding them
# (its parity accounting is scoped to `source='mcp'`), and a cross-tier search must drop
# recall's `embedding_model = $expected_model` filter — so a mismatched card's cosine is
# a comparison between two DIFFERENT vector spaces and carries no information at all.
# The honest value would be 0.0 (discard it), but discarding silently would hide the
# skew from the operator, and the card still has real non-vector content (its structural
# key, its grain, its tier) that a human or a judge can use. So the score is halved:
# enough to keep a mismatched card out of every automatic band (merge 0.95 / conflict
# 0.83 both become unreachable from a cosine ≤ 1.0), while the card itself still
# surfaces with `model_matched=False` for anyone reading the list.
MODEL_MISMATCH_PENALTY = 0.5


@dataclass(frozen=True)
class PriorArtCard:
    """One prior-art hit: what exists, which tier it lives in, and how sure we are.

    Every field is either entity-free by construction or a leakage-gate-scanned surface. All
    members are scalars or tuples of `str`, which is what lets a consumer sort, set-difference or
    join them without a type check at every call site — the mapper guarantees it, not the caller.
    """

    id: str
    kind: PriorArtKind
    tier: PriorArtTier
    status: str
    # TRI-STATE on purpose. `False` means "landed but nobody has verified it";
    # `None` means "the node does not say" — an un-stamped node, which is a DIFFERENT
    # and more alarming thing (a writer that skipped the stamp, or a hand edit). Never
    # coalesce one into the other: `verified is None` is the signal that something wrote
    # this node outside the two writers we control.
    verified: bool | None
    drift_status: str
    intent: str
    result_grain: tuple[str, ...]
    uses_rules: tuple[str, ...]
    structural_key: str
    embedding_model: str
    # The raw score from whatever produced this card. For a vector hit that is the
    # neo4j cosine; for a structural-key hit it is 1.0 (an exact identity).
    similarity: float
    # Whether `embedding_model` equals the model the QUERY was embedded with. Only
    # meaningful when `score_basis == "vector"` — see `confidence`.
    model_matched: bool
    # What produced `similarity`. Load-bearing for `confidence`: a structural-key hit is
    # an exact key identity that involved no vector at all, so the model-skew discount
    # must NOT apply to it (discounting an exact identity would be a straightforward
    # bug, and one that only bites after an embedding-model swap — i.e. long after it
    # was written).
    score_basis: Literal["vector", "structural_key"] = "vector"
    # WHICH store this card came from. Added when the dedup soft layer became a UNION of
    # two sources, and it is not bookkeeping: the two answer different questions and a
    # reader must be able to tell them apart.
    #   graph  — a neo4j node. It exists, it has LANDED, it has a real trust tier.
    #   corpus — a `learning_corpus` artifact. Usually a candidate that has NOT landed
    #            yet, i.e. an in-flight sibling from a concurrent session. Its `tier`
    #            reflects the artifact's stored `source` (learning, in practice), but it
    #            is not a graph node and carries no structural key or `verified` stamp.
    # Only `graph` cards reach `get_by_structural_key`, so only they can ever produce the
    # canon-redundancy drop; a `corpus` card can at most route to a human.
    origin: Literal["graph", "corpus"] = "graph"

    @property
    def confidence(self) -> float:
        """The score a consumer should threshold on — `similarity`, discounted across embedding spaces.

        Computed rather than stored so it can never drift from `model_matched`: there is exactly one
        place the discount is applied, and no way to construct a card whose stored confidence
        disagrees with its stored model flag.
        """
        if self.score_basis != "vector" or self.model_matched:
            return self.similarity
        return self.similarity * MODEL_MISMATCH_PENALTY

    @property
    def is_canon(self) -> bool:
        """True iff this artifact lives in the trusted, git-versioned MCP canon.

        The tier the agent ALREADY recalls, and the only one for which "we re-derived something we
        own" is a statement about RETRIEVAL rather than about the loop.
        """
        return self.tier == TIER_MCP

    @property
    def is_terminal(self) -> bool:
        """True iff a human killed this artifact (rejected/retired).

        Readers filter these out; kept as a property so a card arriving from an unfiltered path is
        still self-describing.
        """
        return self.status in TERMINAL_STATUSES


__all__ = [
    "MODEL_MISMATCH_PENALTY",
    "TERMINAL_STATUSES",
    "TIER_LEARNING",
    "TIER_MCP",
    "TIER_UNSOURCED",
    "PriorArtCard",
    "PriorArtKind",
    "PriorArtTier",
]
