"""QA Layer-1: corpus-loader write-time gates for the windowed-period slots (§1.2).

The new `bp-hires-per-month` (relative_window) and `bp-hires-in-range`
(period_range) seed fixtures load. The token-referencing gates hold for the
period_range TWO-token rule: a template referencing only `{X_start}` is rejected
(the dropped `{X_end}` bound is a dropped filter, D56 class); a bare `{X}` token
for a period_range slot is undeclared; and a required window slot referenced by no
template is rejected.

ADD-only; does not modify the reviewer-owned corpus-loader tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_agent.runtime.blueprint.compiler import validate_blueprint_dag
from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    load_seed_fixtures,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"

_HIRE = "dbpcm_warehouse.employee.MostRecentHireDate"
_CODE = "dbpcm_warehouse.employee.EmployeeCode"


# ===========================================================================
# The new seed fixtures load + validate
# ===========================================================================


def test_windowed_seed_fixtures_present_and_validate() -> None:
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    by_id = {b.id: b for b in blueprints}
    assert "bp-hires-per-month" in by_id
    assert "bp-hires-in-range" in by_id
    # Both validate without raising (the full §1.2 write-time gate).
    validate_blueprint_dag(by_id["bp-hires-per-month"])
    validate_blueprint_dag(by_id["bp-hires-in-range"])


def test_windowed_seed_slot_types() -> None:
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    by_id = {b.id: b for b in blueprints}
    per_month_slots = {s["name"]: s["type"] for s in by_id["bp-hires-per-month"].slots}
    in_range_slots = {s["name"]: s["type"] for s in by_id["bp-hires-in-range"].slots}
    assert per_month_slots == {"window_months": "relative_window"}
    assert in_range_slots == {"hire_window": "period_range"}


# ===========================================================================
# Synthetic seeds — the token-referencing gates
# ===========================================================================


def _range_seed(*, sql_template: str, slots: list[dict[str, object]] | None = None,
                uses: list[str] | None = None) -> BlueprintSeed:
    return BlueprintSeed(
        id="bp-pr",
        intent="hires in a range",
        slots_summary="",
        uses=uses if uses is not None else [_HIRE, _CODE],
        slots=slots if slots is not None
        else [{"name": "hire_window", "type": "period_range", "required": True}],
        result_grain=[],
        sql_template=sql_template,
    )


def _window_seed(*, sql_template: str) -> BlueprintSeed:
    return BlueprintSeed(
        id="bp-rw",
        intent="hires per month",
        slots_summary="",
        uses=[_HIRE, _CODE],
        slots=[{"name": "window_months", "type": "relative_window", "required": True,
                "min_value": 1, "max_value": 36}],
        result_grain=[],
        sql_template=sql_template,
    )


def test_period_range_both_bounds_referenced_validates() -> None:
    # Positive control — BOTH tokens referenced → the seed validates.
    validate_blueprint_dag(
        _range_seed(
            sql_template=(
                "SELECT MostRecentHireDate FROM dbpcm_warehouse.employee "
                "WHERE MostRecentHireDate >= {hire_window_start} "
                "AND MostRecentHireDate < {hire_window_end}"
            )
        )
    )


def test_period_range_only_start_referenced_rejected() -> None:
    # Only {hire_window_start} referenced — the dropped {hire_window_end} bound makes
    # the required slot under-referenced → a dropped filter (D56 class) → reject. The
    # M2 per-node all-or-none gate catches this first (a template referencing ANY
    # range token must reference BOTH), with a more precise half-range message.
    bp = _range_seed(
        sql_template=(
            "SELECT MostRecentHireDate FROM dbpcm_warehouse.employee "
            "WHERE MostRecentHireDate >= {hire_window_start}"
        )
    )
    with pytest.raises(CorpusLoadError, match="only part of period_range slot"):
        validate_blueprint_dag(bp)


def test_period_range_bare_token_rejected_as_undeclared() -> None:
    # A bare {hire_window} is NOT a legal token for a period_range slot (its tokens
    # are {hire_window_start}/{hire_window_end}) → undeclared placeholder.
    bp = _range_seed(
        sql_template=(
            "SELECT MostRecentHireDate FROM dbpcm_warehouse.employee "
            "WHERE MostRecentHireDate >= {hire_window}"
        )
    )
    with pytest.raises(CorpusLoadError, match="undeclared slot"):
        validate_blueprint_dag(bp)


def test_required_relative_window_unreferenced_rejected() -> None:
    # A required relative_window slot whose {window_months} token appears in NO
    # template → an unreferenced required slot (silent dropped filter) → reject.
    bp = _window_seed(
        sql_template="SELECT MostRecentHireDate FROM dbpcm_warehouse.employee"
    )
    with pytest.raises(CorpusLoadError, match="referenced by NO template"):
        validate_blueprint_dag(bp)


def test_relative_window_referenced_validates() -> None:
    # Positive control for relative_window — the {window_months} token is present.
    validate_blueprint_dag(
        _window_seed(
            sql_template=(
                "SELECT MostRecentHireDate FROM dbpcm_warehouse.employee "
                "WHERE MostRecentHireDate >= now() - INTERVAL {window_months} MONTH"
            )
        )
    )
