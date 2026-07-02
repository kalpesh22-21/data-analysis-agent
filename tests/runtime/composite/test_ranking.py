"""Unit tests for composite/ranking.py (Layer 1, pure — design §4)."""

from __future__ import annotations

import math

from data_agent.runtime.composite.ranking import (
    RowValue,
    cosine,
    rank,
    top_margin,
)


def test_cosine_basics() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector guard


def test_rank_semantic_dominant_ordering() -> None:
    rows = [
        RowValue(value="PTO", description="paid time off", freq=10),
        RowValue(value="OT", description="overtime", freq=1000),
    ]
    # Concept vector aligned with PTO; OT is orthogonal.
    concept = [1.0, 0.0]
    row_vectors = [[1.0, 0.0], [0.0, 1.0]]
    ranked = rank(
        rows=rows,
        row_vectors=row_vectors,
        concept_vector=concept,
        similarity_weight=0.7,
        top_k=10,
    )
    # PTO: 0.7*1 + 0.3*(log1p(10)/log1p(1000)) ; OT: 0.7*0 + 0.3*1
    assert ranked[0].value == "PTO"
    assert ranked[1].value == "OT"
    lf_pto = math.log1p(10) / math.log1p(1000)
    assert ranked[0].score == round(0.7 * 1.0 + 0.3 * lf_pto, 4)
    assert ranked[1].score == round(0.3 * 1.0, 4)


def test_rank_equal_freq_similarity_decides() -> None:
    rows = [
        RowValue(value="A", description="alpha", freq=5),
        RowValue(value="B", description="beta", freq=5),
    ]
    concept = [0.0, 1.0]
    row_vectors = [[1.0, 0.0], [0.0, 1.0]]  # B aligns with concept
    ranked = rank(
        rows=rows,
        row_vectors=row_vectors,
        concept_vector=concept,
        similarity_weight=0.7,
        top_k=10,
    )
    assert [r.value for r in ranked] == ["B", "A"]


def test_rank_degraded_is_freq_only() -> None:
    rows = [
        RowValue(value="rare", description="x", freq=1),
        RowValue(value="common", description="y", freq=100),
    ]
    ranked = rank(
        rows=rows,
        row_vectors=None,
        concept_vector=None,
        similarity_weight=0.7,
        top_k=10,
    )
    assert [r.value for r in ranked] == ["common", "rare"]
    assert ranked[0].score == 1.0  # max normalized log-freq
    assert ranked[1].score == round(math.log1p(1) / math.log1p(100), 4)


def test_rank_top_k_truncation() -> None:
    rows = [RowValue(value=f"v{i}", description=None, freq=i + 1) for i in range(20)]
    ranked = rank(
        rows=rows,
        row_vectors=None,
        concept_vector=None,
        similarity_weight=0.7,
        top_k=5,
    )
    assert len(ranked) == 5
    # Highest freq first (freq-only degraded).
    assert ranked[0].value == "v19"


def test_rank_embedding_text_uses_description_when_present() -> None:
    assert RowValue("PTO", "paid time off", 1).embedding_text() == "PTO: paid time off"
    assert RowValue("PTO", None, 1).embedding_text() == "PTO"


def test_top_margin() -> None:
    rows = [
        RowValue(value="A", description=None, freq=100),
        RowValue(value="B", description=None, freq=10),
    ]
    ranked = rank(
        rows=rows, row_vectors=None, concept_vector=None, similarity_weight=0.7, top_k=10
    )
    assert top_margin(ranked) == round(ranked[0].score - ranked[1].score, 4)
    assert top_margin(ranked[:1]) is None
    assert top_margin([]) is None
