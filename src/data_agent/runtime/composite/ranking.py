"""ranking.py — pure ranking of resolved values (design §4). No I/O.

Given the distinct rows returned by the backing `runQuery` (each a
`{value, description, freq}`), their embedding vectors, and the concept vector,
produce the ranked, top-K `ResolvedValue` list the model sees.

Score (design §4.1), per row *i* with distinct `value`, optional `description`,
and `freq`:

    sim_norm_i = max(0.0, cosine(concept, row_i))          # clamp negatives
    lf_i       = log1p(freq_i)
    lf_norm_i  = lf_i / max_j(lf_j)                         # -> [0, 1]
    score_i    = w * sim_norm_i + (1 - w) * lf_norm_i       # w = similarity weight

When embeddings are unavailable (degraded, design §3.2), `row_vectors`/
`concept_vector` are `None` and the score collapses to `lf_norm_i` (w = 0).

Rows are sorted by `score` desc, with `freq` desc then `value` asc as
deterministic tiebreakers, and truncated to `top_k`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RowValue:
    """One distinct row from the backing query (pre-ranking)."""

    value: str
    description: str | None
    freq: int

    def embedding_text(self) -> str:
        """The text embedded for semantic matching (design §4.1)."""
        if self.description:
            return f"{self.value}: {self.description}"
        return self.value


@dataclass(frozen=True)
class ResolvedValue:
    """One ranked value returned to the model — the fixed contract shape."""

    value: str
    description: str | None
    score: float
    freq: int

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "description": self.description,
            "score": self.score,
            "freq": self.freq,
        }


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors; 0.0 if either is zero.

    Kept lenient (`strict=False`): a length mismatch yields a degenerate but
    finite, non-crashing value rather than raising. Vectors are always
    equal-length in practice; the M2 length-mismatch guard lives at the
    composite level (`resolve_values._validate_embed_shape`), which degrades to
    freq-only ranking BEFORE `rank`/`cosine` is ever called on ragged input —
    so this function never has to raise to stay safe.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _normalized_log_freqs(rows: list[RowValue]) -> list[float]:
    log_freqs = [math.log1p(max(0, row.freq)) for row in rows]
    max_lf = max(log_freqs) if log_freqs else 0.0
    if max_lf == 0.0:
        return [0.0 for _ in log_freqs]
    return [lf / max_lf for lf in log_freqs]


def rank(
    *,
    rows: list[RowValue],
    row_vectors: list[list[float]] | None,
    concept_vector: list[float] | None,
    similarity_weight: float,
    top_k: int,
    score_ndigits: int = 4,
) -> list[ResolvedValue]:
    """Rank *rows* into the top-K `ResolvedValue` list (design §4.1).

    Degraded (embeddings unavailable) when *row_vectors* or *concept_vector*
    is `None`: the score is frequency-only.
    """
    degraded = row_vectors is None or concept_vector is None
    lf_norms = _normalized_log_freqs(rows)

    scored: list[ResolvedValue] = []
    for index, row in enumerate(rows):
        lf_norm = lf_norms[index]
        if degraded:
            score = lf_norm
        else:
            assert row_vectors is not None and concept_vector is not None
            sim_norm = max(0.0, cosine(concept_vector, row_vectors[index]))
            score = similarity_weight * sim_norm + (1.0 - similarity_weight) * lf_norm
        scored.append(
            ResolvedValue(
                value=row.value,
                description=row.description,
                score=round(score, score_ndigits),
                freq=row.freq,
            )
        )

    scored.sort(key=lambda rv: (-rv.score, -rv.freq, rv.value))
    return scored[:top_k]


def top_margin(ranked: list[ResolvedValue]) -> float | None:
    """`score[0] - score[1]` — a cheap accept-vs-clarify hint (design §4.2).

    `None` when there are fewer than two results (no margin to compute).
    """
    if len(ranked) < 2:
        return None
    return round(ranked[0].score - ranked[1].score, 4)


__all__ = ["ResolvedValue", "RowValue", "cosine", "rank", "top_margin"]
