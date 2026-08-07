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
    # (an omitted optional slot's optional_pattern is applied at runtime when its
    # token IS referenced; an unreferenced optional slot is simply inert) — must
    # NOT fail the load.
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": True},
            {"name": "maybe", "type": "string", "required": False},
        ]
    )
    _validate_blueprint_dag(bp)  # no raise


# -- Slice C: optional_pattern validated at LOAD (fail loud, not per-hit) ----


def test_valid_optional_pattern_loads() -> None:
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": True},
            {"name": "region", "type": "string", "required": False, "optional_pattern": "TRUE"},
        ]
    )
    _validate_blueprint_dag(bp)  # no raise


def test_malformed_optional_pattern_rejected_at_load() -> None:
    # A non-Condition pattern (a full SELECT) must fail LOUD at load, not burn a
    # fast-path attempt on every hit. (Validated even for an UNREFERENCED optional
    # slot — the pattern itself is checked.)
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": True},
            {"name": "region", "type": "string", "required": False, "optional_pattern": "SELECT 1"},
        ]
    )
    with pytest.raises(CorpusLoadError, match="optional_pattern"):
        _validate_blueprint_dag(bp)


def test_placeholder_bearing_optional_pattern_rejected_at_load() -> None:
    # A pattern carrying a placeholder (`:region`) would loop the runtime apply — the
    # load gate rejects it up front.
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": True},
            {"name": "region", "type": "string", "required": False, "optional_pattern": "b = :region"},
        ]
    )
    with pytest.raises(CorpusLoadError, match="optional_pattern"):
        _validate_blueprint_dag(bp)


# -- e2: a REFERENCED optional slot MUST carry an optional_pattern -----------


def test_referenced_optional_slot_without_pattern_fails_load() -> None:
    # An optional slot whose token IS referenced by the template but that carries NO
    # optional_pattern would leave `{token}` unbound on omission → fast path burns to
    # the raw loop (it does NOT run unfiltered). Fail LOUD at load so the model-facing
    # "omit = all values" note is true by construction.
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": False},  # referenced, no pattern
        ]
    )
    with pytest.raises(CorpusLoadError, match="optional_pattern"):
        _validate_blueprint_dag(bp)


def test_referenced_optional_slot_with_pattern_loads() -> None:
    # The same referenced optional slot WITH an optional_pattern loads fine — on
    # omission the executor substitutes the pattern and the template runs unfiltered.
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": False,
             "optional_pattern": "TRUE"},
        ]
    )
    _validate_blueprint_dag(bp)  # no raise


def test_unreferenced_optional_slot_without_pattern_still_loads() -> None:
    # An UNREFERENCED optional slot needs no pattern — nothing binds its token, so its
    # omission is a genuine no-op. The e2 gate must not touch it (guards the converse
    # of the referenced case above and re-confirms test_unreferenced_optional_slot).
    bp = _seed(
        slots=[
            {"name": "department", "type": "string", "required": True},
            {"name": "maybe", "type": "string", "required": False},  # unreferenced, no pattern
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
        "bp-earnings-by-department-via-scratch-join",
        "bp-hires-per-month",
        "bp-hires-in-range",
        "bp-hires-projection",
        "bp-compare-employee-check-detail-two-periods",
    }
    for bp in blueprints:
        _validate_blueprint_dag(bp)  # no raise — every required slot (incl. both
        # period_range tokens) is referenced, every binds_to ∈ uses, ≤ 16 slots.
