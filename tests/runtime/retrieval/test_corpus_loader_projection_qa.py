"""QA Layer-1: the second-order `bp-hires-projection` seed load-time validation.

The run-rate PROJECTION blueprint (D103/D104, second-order-analysis-design §2-§3)
is a VERIFIED single-row-aggregate blueprint, NOT a bespoke tool. This module pins
the write-time guarantees the corpus loader must hold for it:

  * it loads out of the shipped seed corpus (present in `load_seed_fixtures`);
  * `_validate_blueprint_uses` + `_validate_blueprint_dag` accept it (template
    parses under the ClickHouse dialect, its column footprint ⊆ the declared
    `uses`, both `relative_window` slots are referenced by the template);
  * `result_grain: []` (a single-row aggregate) round-trips as an empty JSON list,
    so the D56 grain gate is vacuously skipped.

ADD-only; mirrors the idiom of `test_corpus_loader_dag.py` and does not touch the
reviewer-owned loader tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    _dag_properties,
    _validate_blueprint_dag,
    _validate_blueprint_uses,
    load_seed_fixtures,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"

_BP_ID = "bp-hires-projection"
_E = "dbpcm_warehouse.employee"
_CODE = f"{_E}.employee_code"
_HIRE = f"{_E}.hire_date"
_STATUS = f"{_E}.employee_status"


def _projection_seed() -> BlueprintSeed:
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    seed = next((b for b in blueprints if b.id == _BP_ID), None)
    assert seed is not None, f"{_BP_ID} missing from the seed corpus"
    return seed


# -- present + validates ----------------------------------------------------


def test_projection_seed_loads_from_corpus() -> None:
    # The whole corpus loads (every seed passes write-time validation) AND the
    # projection seed is one of them — no exception escapes the loader.
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    assert _BP_ID in {b.id for b in blueprints}


def test_projection_seed_passes_uses_and_dag_validation() -> None:
    # The two write-time gates the design (§2) calls out: uses-key shape + the full
    # template/footprint/slot DAG check. Neither raises for the shipped seed.
    seed = _projection_seed()
    _validate_blueprint_uses(seed)  # no raise
    _validate_blueprint_dag(seed)  # no raise


def test_projection_seed_uses_footprint_is_the_two_hire_columns() -> None:
    # The declared footprint is EXACTLY the count column, the hire-date column, and
    # the status column (exclude_not_hired_default drops pre-hire 'N' records in the
    # outer WHERE + the anchor subquery); the DAG validator has already proven the
    # template reads nothing outside it.
    seed = _projection_seed()
    assert set(seed.uses) == {_CODE, _HIRE, _STATUS}


def test_projection_seed_template_references_both_window_slots() -> None:
    # Both `relative_window` slots must be wired into the template (an unreferenced
    # slot, or an undeclared `{token}`, is a load-time CorpusLoadError). window_months
    # drives BOTH the denominator AND the trailing-window INTERVAL; horizon_months the
    # forward multiplier.
    seed = _projection_seed()
    assert seed.sql_template is not None
    slot_names = {s["name"] for s in seed.slots}
    assert slot_names == {"window_months", "horizon_months"}
    for name in slot_names:
        assert "{" + name + "}" in seed.sql_template


def test_projection_seed_window_slot_is_referenced_three_times() -> None:
    # The repeated-slot shape the bind test exercises: {window_months} appears 3x
    # (denominator in avg, denominator in the projection, and the INTERVAL bound)
    # while {horizon_months} appears once (the forward multiplier).
    seed = _projection_seed()
    assert seed.sql_template is not None
    assert seed.sql_template.count("{window_months}") == 3
    assert seed.sql_template.count("{horizon_months}") == 1


def test_projection_seed_result_grain_roundtrips_empty_list() -> None:
    # `result_grain: []` -> a single-row aggregate; D56 grain-verify is vacuously
    # skipped. The property serialization stores it as an empty JSON list (never
    # null, which is the DAG-less legacy shape).
    seed = _projection_seed()
    props = _dag_properties(seed)
    assert props["sql_template"] is not None
    assert json.loads(props["result_grain_json"]) == []


def test_projection_seed_slots_are_bounded_relative_windows() -> None:
    # Both slots are `relative_window` with authored inclusive bounds — the bounds
    # the resolver enforces (window 2..36, horizon 1..24). Pinned so a fixture edit
    # that drops a bound is caught here, not silently at run time.
    seed = _projection_seed()
    by_name = {s["name"]: s for s in seed.slots}
    assert by_name["window_months"]["type"] == "relative_window"
    assert by_name["horizon_months"]["type"] == "relative_window"
    assert (by_name["window_months"]["min_value"], by_name["window_months"]["max_value"]) == (2, 36)
    assert (by_name["horizon_months"]["min_value"], by_name["horizon_months"]["max_value"]) == (
        1,
        24,
    )
