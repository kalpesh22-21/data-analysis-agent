"""PriorArtIndex — the CROSS-TIER "does this already exist?" port (plan §2).

The learning loop's dedup stage historically compared each new candidate against ONE
thing: the `learning_corpus` Couchbase bucket. That bucket is seeded solely by the dedup
stage itself (`DedupStage._seed_on_insert`), so it contains only what the loop has
already minted. The neo4j `source='mcp'` canon the agent actually recalls, and the
`source='learning'` tier the loop has already landed, were both invisible to it — so a
session that re-derived a blueprint we already own produced a duplicate and nothing
noticed.

This port is the missing read. Two questions, deliberately different in kind:

  * `get_by_structural_key` — DETERMINISTIC identity. The loose cross-authoring-path key
    (`runtime/blueprint/structural_key.py`) hashes only the two inputs both the canon
    YAMLs and the learning extractor genuinely have, so a hit is an assertion, not a
    guess. This is the layer allowed to DROP a candidate.
  * `search` — APPROXIMATE nearest neighbour over intent text. A hit is a hint; it can
    route a candidate to a human but must never silently discard one.

**This port RAISES where `runtime/retrieval/vector_index.py::VectorIndex` degrades to
`[]`, and that inversion is deliberate.** Recall's contract is "return what you can, a
missing corpus is an empty corpus" — a degraded recall costs the agent an answer it
might have had. Here an empty list MEANS "no prior art exists", and acting on that when
the truth is "I could not look" is exactly the failure the port was built to fix: the
loop would mint a duplicate of something it already owns and record it as novel. So
infra failure is a distinguishable outcome (`PriorArtUnavailableError`), and every caller is
required to decide what to do about it. The dedup stage's decision is to fall back to
the old corpus-bucket scan and log loudly — never to stop learning.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol

from .models import TERMINAL_STATUSES, PriorArtCard, PriorArtKind


class PriorArtUnavailableError(RuntimeError):
    """The prior-art store could not be consulted (unreachable graph, embed failure,
    query error, timeout).

    Distinct from "no prior art found" on purpose — see the module docstring. A caller
    MUST NOT treat this as an empty result; it means the question was not answered.
    """


class PriorArtIndex(Protocol):
    """The cross-tier prior-art lookup. Implementations return CARDS ONLY (never
    payloads) and never apply recall's `source='mcp'` trust gate."""

    async def search(
        self,
        text: str,
        *,
        kinds: tuple[PriorArtKind, ...] = ("blueprint",),
        limit: int = 5,
    ) -> list[PriorArtCard]:
        """Up to *limit* nearest prior-art cards for *text*, best `confidence` first.

        Spans every trust tier (mcp canon, learning staging, and any unsourced node)
        and every embedding model — the model-parity filter is dropped so a corpus the
        hydrator left un-re-embedded is still visible, with `model_matched=False` and a
        discounted `confidence` rather than a silently-trusted cross-space cosine.

        Terminal (`rejected`/`retired`) artifacts are excluded: a human already declined
        those, and surfacing them would resurrect a settled decision forever.

        Raises:
            PriorArtUnavailableError: the store could not be consulted. NOT the same as `[]`.
        """
        ...

    async def get_by_structural_key(self, key: str) -> PriorArtCard | None:
        """The artifact carrying this exact `structural_key`, or `None` on a genuine
        miss. When BOTH tiers carry the key the MCP-canon node wins — "the canon already
        has this" is the stronger and more actionable answer.

        An empty *key* is a MISS, never a query: `structural_key_from_templates` returns
        `""` when a template does not normalize, and matching every un-keyed node
        against each other would be the worst possible false positive.

        Raises:
            PriorArtUnavailableError: the store could not be consulted.
        """
        ...


# --------------------------------------------------------------------------
# InMemoryPriorArtIndex — the Layer-1 fake (house convention: see
# `dedup/corpus.py::InMemoryBlueprintCorpus`, `candidate/memory_candidate_store.py`)
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> frozenset[str]:
    folded = unicodedata.normalize("NFC", text).lower()
    return frozenset(_TOKEN_RE.findall(folded))


def token_overlap(query: str, card: PriorArtCard) -> float:
    """Jaccard overlap of *query*'s tokens with the card's intent — the in-memory
    fake's default ranking.

    Deliberately NOT a fake cosine: a test that wants an exact similarity should script
    it (`scores=`), and a test that just wants "these two intents are obviously about
    the same thing" gets a monotone, deterministic, dependency-free answer here. It has
    no vector space, so `model_matched` is meaningless in the fake and every seeded card
    keeps whatever flag the test gave it.
    """
    q = _tokens(query)
    c = _tokens(card.intent)
    if not q or not c:
        return 0.0
    return len(q & c) / len(q | c)


class InMemoryPriorArtIndex:
    """Dict/list-backed `PriorArtIndex` fake — no I/O, deterministic, Layer-1 only.

    Seed it with cards. Ranking is `token_overlap` unless a test pins exact scores via
    `scores={(query_text, card_id): 0.97}`, which is how a band-boundary test states its
    intent without smuggling a vector in. `fail=True` makes BOTH methods raise
    `PriorArtUnavailableError`, so a caller's fail-open path is exercisable with no infra.

    Terminal-status filtering happens HERE too, not only in the real reader: the whole
    point of the fake is that a caller cannot tell the two apart, and a filter that
    exists in one and not the other is a bug the unit suite would happily miss.
    """

    def __init__(
        self,
        cards: list[PriorArtCard] | None = None,
        *,
        scores: dict[tuple[str, str], float] | None = None,
        fail: bool = False,
    ) -> None:
        self._cards: list[PriorArtCard] = list(cards or [])
        self._scores = dict(scores or {})
        self._fail = fail
        # Audit trail for tests: the queries this index was asked, in order.
        self.search_calls: list[tuple[str, tuple[str, ...], int]] = []
        self.key_calls: list[str] = []

    def add(self, card: PriorArtCard) -> None:
        self._cards.append(card)

    async def search(
        self,
        text: str,
        *,
        kinds: tuple[PriorArtKind, ...] = ("blueprint",),
        limit: int = 5,
    ) -> list[PriorArtCard]:
        self.search_calls.append((text, tuple(kinds), limit))
        if self._fail:
            raise PriorArtUnavailableError("in-memory prior-art index scripted to fail")
        scored: list[PriorArtCard] = []
        for card in self._cards:
            if card.kind not in kinds or card.status in TERMINAL_STATUSES:
                continue
            sim = self._scores.get((text, card.id))
            if sim is None:
                sim = token_overlap(text, card)
            scored.append(_with_similarity(card, sim))
        # Descending confidence with a stable id tiebreak — the same determinism
        # `FakeVectorIndex.recall` provides, for the same reason.
        scored.sort(key=lambda c: (-c.confidence, c.id))
        return scored[:limit]

    async def get_by_structural_key(self, key: str) -> PriorArtCard | None:
        self.key_calls.append(key)
        if self._fail:
            raise PriorArtUnavailableError("in-memory prior-art index scripted to fail")
        if not key:
            return None  # an absent key matches nothing — never "matches everything"
        matches = [
            card
            for card in self._cards
            if card.structural_key == key and card.status not in TERMINAL_STATUSES
        ]
        if not matches:
            return None
        # Canon wins, then a stable id tiebreak — mirrors the real reader's ORDER BY.
        matches.sort(key=lambda c: (0 if c.is_canon else 1, c.id))
        best = matches[0]
        return _with_similarity(best, 1.0, basis="structural_key")


def _with_similarity(
    card: PriorArtCard, similarity: float, *, basis: str = "vector"
) -> PriorArtCard:
    from dataclasses import replace

    return replace(card, similarity=similarity, score_basis=basis)  # type: ignore[arg-type]


__all__ = [
    "InMemoryPriorArtIndex",
    "PriorArtIndex",
    "PriorArtUnavailableError",
    "token_overlap",
]
