"""Adversarial Layer-1 tests for composite/ranking.py (D77, design §4).

Covers ranking edge cases the happy-path suite misses: NaN / zero vectors,
all-identical similarities, ties -> deterministic order, top_k boundaries
(> row count, 0, negative), weight extremes (0.0 / 1.0), all-equal freq, and
negative freq guarding.
"""

from __future__ import annotations

import math

from data_agent.runtime.composite.ranking import RowValue, cosine, rank, top_margin

_NAN = float("nan")


def _rows(*specs: tuple[str, int]) -> list[RowValue]:
    return [RowValue(value=v, description=None, freq=f) for v, f in specs]


# --- cosine edge cases ------------------------------------------------------


def test_cosine_nan_vector_is_not_error() -> None:
    # A NaN component yields a NaN cosine; ranking clamps it (below).
    result = cosine([_NAN, 1.0], [1.0, 0.0])
    assert math.isnan(result)


def test_cosine_zero_vector_returns_zero() -> None:
    assert cosine([0.0, 0.0], [0.5, 0.5]) == 0.0
    assert cosine([0.5, 0.5], [0.0, 0.0]) == 0.0


def test_cosine_mismatched_length_does_not_crash() -> None:
    # zip(strict=False) truncates the dot product to the shorter length, but the
    # norms still span each full vector — so a mismatch yields a degenerate (yet
    # finite, non-crashing) value. Vectors are always equal-length in practice;
    # this documents defensive non-crash behavior only.
    result = cosine([1.0, 0.0, 999.0], [1.0, 0.0])
    assert math.isfinite(result)
    assert 0.0 <= result <= 1.0


# --- NaN / zero vectors through rank() --------------------------------------


def test_rank_nan_similarity_clamped_to_zero_deterministic() -> None:
    rows = _rows(("A", 10), ("B", 100))
    ranked = rank(
        rows=rows,
        row_vectors=[[_NAN, _NAN], [_NAN, _NAN]],
        concept_vector=[1.0, 0.0],
        similarity_weight=0.7,
        top_k=10,
    )
    # NaN sims clamp to 0.0 -> score is (1-w)*lf_norm only; higher freq wins,
    # and the order is fully deterministic (no NaN leaking into the sort key).
    assert [r.value for r in ranked] == ["B", "A"]
    assert all(not math.isnan(r.score) for r in ranked)


def test_rank_zero_concept_vector_collapses_to_freq_order() -> None:
    rows = _rows(("A", 10), ("B", 100))
    ranked = rank(
        rows=rows,
        row_vectors=[[1.0, 0.0], [0.0, 1.0]],
        concept_vector=[0.0, 0.0],  # zero concept -> all sims 0
        similarity_weight=0.7,
        top_k=10,
    )
    assert [r.value for r in ranked] == ["B", "A"]


# --- all-identical similarities / ties / determinism ------------------------


def test_rank_identical_similarities_break_by_freq_then_value() -> None:
    rows = _rows(("banana", 5), ("apple", 5), ("cherry", 9))
    # All rows share the same vector -> identical similarity to the concept.
    same = [1.0, 0.0]
    ranked = rank(
        rows=rows,
        row_vectors=[same, same, same],
        concept_vector=[1.0, 0.0],
        similarity_weight=0.7,
        top_k=10,
    )
    # cherry (freq 9) first; then the freq-5 tie breaks by value ascending.
    assert [r.value for r in ranked] == ["cherry", "apple", "banana"]


def test_rank_total_ties_break_by_value_ascending_deterministic() -> None:
    rows = _rows(("zeta", 5), ("alpha", 5), ("mu", 5))
    same = [1.0, 0.0]
    ranked = rank(
        rows=rows,
        row_vectors=[same, same, same],
        concept_vector=[1.0, 0.0],
        similarity_weight=0.7,
        top_k=10,
    )
    assert [r.value for r in ranked] == ["alpha", "mu", "zeta"]


def test_rank_single_row() -> None:
    ranked = rank(
        rows=_rows(("only", 3)),
        row_vectors=[[1.0, 0.0]],
        concept_vector=[1.0, 0.0],
        similarity_weight=0.7,
        top_k=10,
    )
    assert len(ranked) == 1
    assert ranked[0].value == "only"
    assert top_margin(ranked) is None


# --- top_k boundaries -------------------------------------------------------


def test_rank_top_k_greater_than_row_count_returns_all() -> None:
    ranked = rank(
        rows=_rows(("A", 1), ("B", 2)),
        row_vectors=None,
        concept_vector=None,
        similarity_weight=0.7,
        top_k=100,
    )
    assert len(ranked) == 2


def test_rank_top_k_zero_returns_empty() -> None:
    ranked = rank(
        rows=_rows(("A", 1), ("B", 2)),
        row_vectors=None,
        concept_vector=None,
        similarity_weight=0.7,
        top_k=0,
    )
    assert ranked == []


def test_rank_negative_top_k_slices_from_end() -> None:
    # Documents current behavior: top_k is a raw slice bound; a negative value
    # drops from the end (settings enforce ge=1, so this is defensive doc only).
    ranked = rank(
        rows=_rows(("A", 1), ("B", 2), ("C", 3)),
        row_vectors=None,
        concept_vector=None,
        similarity_weight=0.7,
        top_k=-1,
    )
    # sorted desc by freq: C, B, A -> [:-1] drops A.
    assert [r.value for r in ranked] == ["C", "B"]


# --- weight extremes --------------------------------------------------------


def test_rank_weight_one_is_pure_similarity() -> None:
    rows = _rows(("low_sim_high_freq", 1000), ("high_sim_low_freq", 1))
    ranked = rank(
        rows=rows,
        row_vectors=[[0.0, 1.0], [1.0, 0.0]],
        concept_vector=[1.0, 0.0],
        similarity_weight=1.0,
        top_k=10,
    )
    # w=1.0 -> freq ignored entirely; the semantically-aligned row wins.
    assert ranked[0].value == "high_sim_low_freq"
    assert ranked[0].score == 1.0
    assert ranked[1].score == 0.0


def test_rank_weight_zero_is_pure_freq() -> None:
    rows = _rows(("high_sim_low_freq", 1), ("low_sim_high_freq", 1000))
    ranked = rank(
        rows=rows,
        row_vectors=[[1.0, 0.0], [0.0, 1.0]],
        concept_vector=[1.0, 0.0],
        similarity_weight=0.0,
        top_k=10,
    )
    # w=0.0 -> similarity ignored; frequency decides.
    assert ranked[0].value == "low_sim_high_freq"


# --- freq edge cases --------------------------------------------------------


def test_rank_all_equal_freq_similarity_fully_decides() -> None:
    rows = _rows(("A", 7), ("B", 7))
    ranked = rank(
        rows=rows,
        row_vectors=[[0.0, 1.0], [1.0, 0.0]],
        concept_vector=[1.0, 0.0],  # aligns with B
        similarity_weight=0.7,
        top_k=10,
    )
    assert [r.value for r in ranked] == ["B", "A"]
    # Equal freq -> lf_norm is 1.0 for both; the (1-w) term is a constant.
    assert ranked[0].score == round(0.7 * 1.0 + 0.3 * 1.0, 4)


def test_rank_zero_freq_all_rows_no_divide_by_zero() -> None:
    rows = _rows(("A", 0), ("B", 0))
    ranked = rank(
        rows=rows,
        row_vectors=None,
        concept_vector=None,
        similarity_weight=0.7,
        top_k=10,
    )
    # max log-freq is 0 -> normalized log-freqs are all 0.0, no ZeroDivisionError.
    assert all(r.score == 0.0 for r in ranked)


def test_rank_negative_freq_is_guarded() -> None:
    # A malformed negative freq must not blow up log1p (log1p(-1) is -inf,
    # log1p(<-1) is a domain error) — _normalized_log_freqs clamps via max(0, .).
    rows = _rows(("neg", -5), ("pos", 10))
    ranked = rank(
        rows=rows,
        row_vectors=None,
        concept_vector=None,
        similarity_weight=0.7,
        top_k=10,
    )
    assert [r.value for r in ranked] == ["pos", "neg"]
    assert all(math.isfinite(r.score) for r in ranked)
