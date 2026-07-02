"""Layer-1: Slice-B REVIEW fixes at the corpus-loader write gate (runblueprint-design
§1.2, reviewer B3(a) + S1 + S3).

  - B3(a): a declared REQUIRED slot that NO template references ⇒ CorpusLoadError
    (the converse of the token⊆slots check — an unreferenced required slot is a
    silent dropped filter, the D56 wrong-answer class).
  - S1: a slot whose `binds_to` is NOT within the declared `uses` ⇒ CorpusLoadError
    (the DISTINCT domain probe must read only an advertised column).
  - S3: >16 declared slots ⇒ CorpusLoadError (the models slot cap, surfaced as a
    malformed-DAG load error — bounds unbudgeted inner probes on a poisoned record).
  - the 3 seed fixtures STILL load under all three new checks (no hidden bug).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_agent.runtime.retrieval.corpus_loader import (
    BlueprintSeed,
    CorpusLoadError,
    _validate_blueprint_dag,
    load_seed_fixtures,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "corpus"
_E = "dbpcm_warehouse.employee"


def _seed(**overrides: object) -> BlueprintSeed:
    base = {
        "id": "bp-x",
        "intent": "x",
        "slots_summary": "department",
        "uses": [f"{_E}.Department", f"{_E}.EmployeeCode"],
        "slots": [{"name": "department", "type": "string", "required": True}],
        "result_grain": ["Department"],
        "sql_template": (
            "SELECT Department FROM dbpcm_warehouse.employee WHERE Department = {department}"
        ),
    }
    base.update(overrides)
    return BlueprintSeed(**base)  # type: ignore[arg-type]


# -- B3(a): required slot referenced by ≥1 template -------------------------


def test_unreferenced_required_slot_fails_load() -> None:
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": True},
            {"name": "ghost", "type": "string", "required": True},  # never in the template
        ]
    )
    with pytest.raises(CorpusLoadError, match="ghost"):
        _validate_blueprint_dag(bp)


def test_unreferenced_optional_slot_is_allowed() -> None:
    # Only REQUIRED slots must be referenced; an optional slot may be unreferenced
    # (its optional_pattern assembly is Slice C) — must NOT fail the load.
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": True},
            {"name": "maybe", "type": "string", "required": False},
        ]
    )
    _validate_blueprint_dag(bp)  # no raise


# -- S1: binds_to ⊆ uses ----------------------------------------------------


def test_binds_to_outside_uses_fails_load() -> None:
    # `binds_to` points at a column NOT in `uses` (and not read by the template, so
    # the footprint check would pass) — S1 catches it so a probe can never read an
    # unadvertised column.
    bp = _seed(
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": f"{_E}.SecretCol"}]
    )
    with pytest.raises(CorpusLoadError, match="binds_to"):
        _validate_blueprint_dag(bp)


def test_binds_to_within_uses_passes() -> None:
    bp = _seed(
        uses=[f"{_E}.Department", f"{_E}.EmployeeCode"],
        slots=[{"name": "department", "type": "string", "required": True, "binds_to": f"{_E}.Department"}],
    )
    _validate_blueprint_dag(bp)  # no raise


# -- S3: slot cap -----------------------------------------------------------


def test_too_many_slots_fails_load() -> None:
    many = [{"name": f"s{i}", "type": "string", "required": False} for i in range(17)]
    bp = _seed(slots=many)
    with pytest.raises(CorpusLoadError, match="slot"):
        _validate_blueprint_dag(bp)


# -- the 3 seed fixtures still load (the B3 fixture check) -------------------


def test_all_seed_fixtures_still_validate_under_b3_s1_s3() -> None:
    blueprints, _ = load_seed_fixtures(_FIXTURE_DIR)
    assert {b.id for b in blueprints} == {
        "bp-overtime-by-department",
        "bp-active-headcount-by-department",
        "bp-average-salary-by-department",
        "bp-total-earnings-by-department",
        "bp-departments-above-company-average-salary",
    }
    for bp in blueprints:
        _validate_blueprint_dag(bp)  # no raise — every required slot is referenced,
        # every binds_to ∈ uses, and each has ≤ 16 slots.
