"""VectorIndex — the recall seam (design §2/§3.2). Slice 1 ships the fake only.

The vector store is settled as neo4j-native (D60); this module defines only the
`VectorIndex` Protocol the pipeline depends on plus an in-memory `FakeVectorIndex`
for Layer-1 and the Layer-2 embed→rerank end-to-end tests. `Neo4jVectorIndex` is
Slice 2 (design §4.3) and is deliberately NOT built here — Slice 1 proves the
whole embed→rerank→inject path against the real D71 clients with zero graph-store
risk.

`recall` is order-preserving by descending similarity and sets each returned
`Candidate.score` to its recall similarity, so the "no reranker" degrade path
(design §2) keeps a meaningful recall-order score without re-computing anything.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from data_agent.runtime.composite.ranking import cosine

from .models import Candidate


class VectorIndex(Protocol):
    """The recall seam. Phase-1 real impl is `Neo4jVectorIndex` (Slice 2)."""

    async def recall(
        self, *, query_vector: list[float], kind: str, k: int
    ) -> list[Candidate]:
        """Return up to *k* nearest `Candidate`s of *kind*, most-similar first.

        The real (neo4j) impl reads the `embedding_model` stamped on stored
        vectors and returns `[]` (with a warn span attr) on a model-id mismatch
        rather than garbage neighbours (design §2). An unavailable/empty index
        returns `[]` — a per-corpus degrade that never fails the turn.
        """
        ...


class FakeVectorIndex:
    """Layer-1 `VectorIndex` double — an in-memory corpus ranked by cosine.

    Seeded per test with `(Candidate, vector)` pairs. `recall` filters by
    `kind`, ranks by `cosine(query_vector, vector)` descending, and returns the
    top-*k* with `Candidate.score` set to the similarity. Deterministic; no
    network. A `fail=True` flag makes `recall` return `[]` for every corpus, to
    exercise the pipeline's "index unavailable/empty" degrade (design §2).
    """

    def __init__(
        self,
        entries: list[tuple[Candidate, list[float]]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self._entries: list[tuple[Candidate, list[float]]] = list(entries or [])
        self._fail = fail
        self.calls: list[tuple[str, int]] = []

    def add(self, candidate: Candidate, vector: list[float]) -> None:
        """Seed one candidate and its stored vector into the fake index."""
        self._entries.append((candidate, vector))

    async def recall(
        self, *, query_vector: list[float], kind: str, k: int
    ) -> list[Candidate]:
        self.calls.append((kind, k))
        if self._fail:
            return []
        scored: list[tuple[float, Candidate]] = [
            (cosine(query_vector, vector), candidate)
            for candidate, vector in self._entries
            if candidate.kind == kind
        ]
        # Descending similarity; a stable tiebreak on id keeps recall order
        # deterministic for candidates with identical similarity.
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [replace(candidate, score=sim) for sim, candidate in scored[:k]]


__all__ = ["FakeVectorIndex", "VectorIndex"]
